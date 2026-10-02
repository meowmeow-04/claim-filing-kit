"""Live voice capture: speak into the microphone and watch Gnani's transcript appear.

    GNANI_API_KEY=your-key python -m agenticorg_gnani.voice_server
    then open http://localhost:8765 and press "Start recording"

How it fits together:

    browser page  --audio-->  this server  --audio-->  Gnani realtime
                  <--text---               <--text---

The page cannot talk to Gnani itself: a browser cannot set the header the key
travels in, and the key must not be handed to a web page anyway. So this small
server sits in between. It holds the key, re-cuts the audio into the exact
frames Gnani wants, keeps them at speaking speed, and when the speaker stops
it saves the transcript as a "voice session" file. The Gnani connector's
``query`` tool reads that file for workflow state 1.

It listens on this machine only (127.0.0.1). A browser allows microphone use
on ``localhost`` without https; anywhere else needs https and a sign-in in
front, which this server does not provide.

Needs only ``websockets``. It imports nothing from AgenticOrg.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import wave
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from agenticorg_gnani.realtime import (
    DEFAULT_BASE_URL,
    FRAME_SECONDS,
    MAX_SESSION_SECONDS,
    SAMPLE_RATE,
    SUPPORTED_LANGUAGES,
    Framer,
    RealtimeError,
    RealtimeRefused,
    RealtimeSession,
    stream_url,
)
from agenticorg_gnani.sessions import SOURCE, new_session_id, save_session, sessions_dir

logger = logging.getLogger("agenticorg_gnani.voice")

PAGE = Path(__file__).resolve().parent / "static" / "voice.html"
LANGUAGE_NAMES = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "bn-IN": "Bengali",
    "gu-IN": "Gujarati",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
}
_LOOPBACK = ("127.0.0.1", "localhost")
_BACKLOG_SECONDS = 15  # audio waiting to go to Gnani beyond this is dropped, and the session marked incomplete


@dataclass
class VoiceConfig:
    api_key: str = field(repr=False)  # never shown when the config is printed or logged
    base_url: str = DEFAULT_BASE_URL
    host: str = "127.0.0.1"
    port: int = 8765
    sessions_dir: Path = field(default_factory=sessions_dir)
    max_seconds: int = 300
    save_audio: bool = False
    max_sessions: int = 2
    # Frames from the browser are passed on no closer together than this. Speaking
    # speed is one frame per 32 ms; the slack lets a short backlog catch up.
    min_frame_gap: float = FRAME_SECONDS * 0.9
    finish: dict[str, float] = field(default_factory=dict)  # tests shorten RealtimeSession.finish's waits

    def __post_init__(self) -> None:
        if self.host not in _LOOPBACK:
            raise ValueError(
                "the voice server only listens on this machine (127.0.0.1). To let other people use it, "
                "put it behind an https address with a sign-in; it has neither."
            )
        if not 5 <= self.max_seconds <= MAX_SESSION_SECONDS - 60:
            raise ValueError(f"max_seconds must be between 5 and {MAX_SESSION_SECONDS - 60}")
        stream_url(self.base_url)  # raises on a non-https address


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _http(status: HTTPStatus, body: str | bytes, content_type: str) -> Response:
    data = body.encode() if isinstance(body, str) else body
    headers = Headers(
        {
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        }
    )
    return Response(status.value, status.phrase, headers, data)


class VoiceServer:
    def __init__(self, config: VoiceConfig, on_saved: Callable[[dict[str, Any], Path], None] | None = None) -> None:
        self.config = config
        self.on_saved = on_saved
        self.port = config.port
        self._active = 0
        self._server: Any = None

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            self.config.host,
            self.config.port,
            process_request=self._route,
            max_size=64 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    # -------------------------------------------------------------------- http

    def _origins(self) -> set[str]:
        return {f"http://{host}:{self.port}" for host in _LOOPBACK}

    def _route(self, _connection: Any, request: Any) -> Response | None:
        path = request.path.split("?", 1)[0]
        if path == "/ws":
            # Only this server's own page may open the audio socket. Without this,
            # any website open in the same browser could use the Gnani key through it.
            if request.headers.get("Origin") not in self._origins():
                return _http(HTTPStatus.FORBIDDEN, "This socket only accepts the capture page.\n", "text/plain")
            return None
        if path in ("/", "/index.html"):
            settings = {
                "languages": [{"code": code, "name": LANGUAGE_NAMES[code]} for code in LANGUAGE_NAMES],
                "maxSeconds": self.config.max_seconds,
            }
            page = PAGE.read_text(encoding="utf-8").replace("__SETTINGS__", json.dumps(settings))
            return _http(HTTPStatus.OK, page, "text/html; charset=utf-8")
        if path == "/healthz":
            return _http(HTTPStatus.OK, json.dumps({"ok": True, "active_sessions": self._active}), "application/json")
        return _http(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain")

    # ------------------------------------------------------------------ socket

    async def _handle(self, ws: Any) -> None:
        if self._active >= self.config.max_sessions:
            await self._tell(ws, type="error", message="Another recording is already in progress. Try again shortly.")
            return
        self._active += 1
        try:
            await self._capture(ws)
        except ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001 - one broken session must not take the server down
            logger.exception("voice session failed")
            await self._tell(ws, type="error", message="The recording stopped because of an internal error.")
        finally:
            self._active -= 1

    @staticmethod
    async def _tell(ws: Any, **message: Any) -> bool:
        try:
            await ws.send(json.dumps(message, ensure_ascii=False))
        except ConnectionClosed:
            return False
        return True

    async def _capture(self, ws: Any) -> None:
        config = self.config
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=15)
            start = json.loads(first) if isinstance(first, str) else {}
        except (TimeoutError, ValueError):
            start = {}
        if not isinstance(start, dict) or start.get("type") != "start":
            await self._tell(ws, type="error", message="The page did not start the recording properly. Reload it.")
            return
        language = str(start.get("language") or "en-IN")
        if language not in SUPPORTED_LANGUAGES:
            await self._tell(ws, type="error", message=f"Language {language!r} is not supported.")
            return

        try:
            gnani = await RealtimeSession.open(config.api_key, language, base_url=config.base_url)
        except RealtimeRefused as exc:
            logger.warning("Gnani refused the realtime session: %s", exc)
            await self._tell(ws, type="error", message=f"Gnani did not accept the connection. {exc}")
            return
        except RealtimeError as exc:
            logger.warning("Gnani could not be reached: %s", exc)
            await self._tell(ws, type="error", message=f"Could not reach Gnani. {exc}")
            return

        session_id = new_session_id()
        started_at = _now()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.max_seconds
        # Unbounded, with the limit applied in take(): the end marker must always fit,
        # even when Gnani has stopped taking audio and the backlog is full.
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        backlog_limit = int(_BACKLOG_SECONDS / FRAME_SECONDS)
        framer = Framer()
        audio = bytearray()
        frames_in = dropped = 0
        ended = "stopped"
        stalled = False

        def take(frames: list[bytes]) -> int:
            """Queue frames for Gnani; returns how many did not fit."""
            lost = 0
            for frame in frames:
                if config.save_audio:
                    audio.extend(frame)
                if queue.qsize() >= backlog_limit:
                    lost += 1
                else:
                    queue.put_nowait(frame)
            return lost

        relay = asyncio.create_task(self._relay_events(ws, gnani))
        sender = asyncio.create_task(self._send_audio(gnani, queue))
        await self._tell(ws, type="ready", session_id=session_id, max_seconds=config.max_seconds)

        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    ended = "time_limit"
                    await self._tell(ws, type="stopping", message="Time limit reached. Saving what you said.")
                    break
                if sender.done():  # the connection to Gnani dropped
                    ended = "gnani_closed"
                    break
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 0.5))
                except TimeoutError:
                    continue
                except ConnectionClosed:
                    ended = "disconnected"
                    break
                if isinstance(message, str):
                    try:
                        if json.loads(message).get("type") == "stop":
                            break
                    except (ValueError, AttributeError):
                        pass
                    continue
                frames = framer.feed(message)
                frames_in, dropped = frames_in + len(frames), dropped + take(frames)
        finally:
            frames = framer.flush()  # the part-frame in hand when the speaker stopped, padded with silence
            frames_in, dropped = frames_in + len(frames), dropped + take(frames)
            queue.put_nowait(None)
            # Let the backlog go out. A sender stuck on a dead connection is not waited for.
            stalled = not await self._settle(sender, queue.qsize() * config.min_frame_gap + 5)
            await gnani.finish(**config.finish)
            await self._settle(relay, 10)

        transcript = gnani.transcript
        problems = [*gnani.errors, *([gnani.closed_reason] if gnani.closed_reason else [])]
        if gnani.flush_timed_out:
            problems.append("Gnani did not return the last sentence in time")
        if stalled:
            problems.append("Gnani stopped taking audio before the end")
        if dropped:
            problems.append(f"{dropped} audio frames were dropped (the page sent too fast)")
        session: dict[str, Any] = {
            "session_id": session_id,
            "source": SOURCE,
            "language_code": gnani.language,
            "format": "verbatim",
            "started_at": started_at,
            "ended_at": _now(),
            "ended": ended,
            "complete": ended in ("stopped", "time_limit") and not (problems or dropped or stalled),
            "audio_seconds": round(frames_in * FRAME_SECONDS, 2),
            "request_id": gnani.request_id,
            "segments": gnani.segments,
            "transcript": transcript,
        }
        if problems:
            session["problems"] = problems

        if not transcript:
            reason = "; ".join(session.get("problems", [])) or "No speech was recognised."
            await self._tell(
                ws, type="error", message=f"{reason} Check the microphone and the language, then try again."
            )
            logger.info("voice session ended with no transcript (%s, %.1f s)", ended, session["audio_seconds"])
            return

        if config.save_audio and audio:
            session["audio_file"] = f"{session_id}.wav"
        path = await asyncio.to_thread(self._save, session, bytes(audio))
        await self._tell(
            ws,
            type="saved",
            session_id=session_id,
            transcript=transcript,
            segments=len(gnani.segments),
            audio_seconds=session["audio_seconds"],
            complete=session["complete"],
        )
        if self.on_saved:
            self.on_saved(session, path)

    def _save(self, session: dict[str, Any], audio: bytes) -> Path:
        directory = self.config.sessions_dir
        path = save_session(directory, session)
        if session.get("audio_file"):
            target = directory / session["audio_file"]
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as raw, wave.open(raw, "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(SAMPLE_RATE)
                out.writeframes(audio)
        return path

    @staticmethod
    async def _settle(task: asyncio.Task[None], seconds: float) -> bool:
        """Wait for a task to end; cancel it if it has not after ``seconds``. True when it ended by itself."""
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return bool(done)

    async def _relay_events(self, ws: Any, gnani: RealtimeSession) -> None:
        """Pass Gnani's messages on to the page as they arrive."""
        async for event in gnani.stream():
            kind = event.get("type")
            if kind == "transcript":
                await self._tell(
                    ws, type="transcript", index=len(gnani.segments) - 1, text=str(event.get("text") or "").strip()
                )
            elif kind == "processing":
                await self._tell(ws, type="processing")
            elif kind == "error":
                await self._tell(ws, type="notice", message=f"Gnani reported a problem: {event.get('message')}")

    async def _send_audio(self, gnani: RealtimeSession, queue: asyncio.Queue[bytes | None]) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while True:
            frame = await queue.get()
            if frame is None:
                return
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await gnani.send_frame(frame)
            except RealtimeError:
                return
            next_at = loop.time() + self.config.min_frame_gap


# ------------------------------------------------------------------------ cli


def _announce(session: dict[str, Any], path: Path) -> None:
    print(
        f"\nSaved voice session {session['session_id']}: {len(session['segments'])} segment(s), "
        f"{session['audio_seconds']:.0f} s, {session['language_code']}\n"
        f"  file: {path}\n"
        f"  use it in the workflow:  --voice-session {session['session_id']}",
        flush=True,
    )


async def _serve(config: VoiceConfig, open_browser: bool) -> None:
    server = VoiceServer(config, on_saved=_announce)
    await server.start()
    print(f"Live voice capture is running. Open {server.url} in Chrome, Edge, Safari or Firefox.")
    print(f"Voice sessions are saved in {config.sessions_dir}. Press Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(server.url)
    try:
        await asyncio.Future()
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live voice capture with Gnani realtime speech-to-text")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--sessions-dir", default=os.environ.get("GNANI_SESSIONS_DIR", ""), help="default: ~/gnani-voice-sessions"
    )
    parser.add_argument(
        "--max-seconds", type=int, default=300, help="longest recording; Gnani's own limit is 15 minutes"
    )
    parser.add_argument("--save-audio", action="store_true", help="also keep the recording as a WAV file")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    key = os.environ.get("GNANI_API_KEY", "").strip()
    if not key:
        print("Set GNANI_API_KEY first:  export GNANI_API_KEY='your-key'")
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        config = VoiceConfig(
            api_key=key,
            base_url=os.environ.get("GNANI_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            port=args.port,
            sessions_dir=sessions_dir(args.sessions_dir),
            max_seconds=args.max_seconds,
            save_audio=args.save_audio,
        )
    except ValueError as exc:
        print(f"Cannot start: {exc}")
        return 2
    try:
        asyncio.run(_serve(config, open_browser=not args.no_open))
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as exc:
        print(f"Cannot start on port {args.port}: {exc}. Try --port 8766.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
