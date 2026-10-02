"""Client for Gnani's realtime speech-to-text WebSocket.

Protocol, from https://docs.gnani.ai/api/STT/stt-websocket and Gnani's own
SDK (``gnani-vachana`` 0.7.9, ``GnaniSTTStreamClient``):

    wss://api.vachana.ai/stt/v3/stream
    handshake headers   x-api-key-id, lang_code, x-sample-rate, x-api-request-id
                        [x-format: transcribe] [itn_native_numerals: true]
    client -> server    binary frames of exactly 1024 bytes: 512 samples of
                        16-bit little-endian mono PCM, sent at speaking speed
                        (one frame every 32 ms at 16 kHz)
    server -> client    JSON text: connected, processing, transcript, error
    limits              one language per session; 15 minutes per session

The key is the same one the REST endpoint takes; only the header's case differs.

Transcripts are final: one arrives each time the speaker pauses (about half a
second of silence). There is no "end of audio" message, so :meth:`finish`
keeps sending silence until the last segment has come back.

This module needs only ``websockets``. It imports nothing from AgenticOrg.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import uuid
from collections.abc import AsyncIterator, Iterable
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

DEFAULT_BASE_URL = "https://api.vachana.ai"
STREAM_PATH = "/stt/v3/stream"
SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE  # 0.032
SILENT_FRAME = b"\x00" * FRAME_BYTES
MAX_SESSION_SECONDS = 15 * 60  # Gnani's cap

SUPPORTED_LANGUAGES = (
    "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN",
    "ml-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN",
)  # fmt: skip

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SEGMENT_CLOSING = frozenset({"processing", "speech_end", "vad_end"})


class _ConnectWithoutRedirects(connect):
    """``connect`` that never follows a redirect.

    The library would repeat the handshake, key header included, at whatever
    address a 3xx answer names. The key goes to the configured host only, so a
    redirect is reported as a refusal instead.
    """

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


class RealtimeError(Exception):
    """The realtime session could not be opened or broke."""


class RealtimeRefused(RealtimeError):  # noqa: N818 - reads better at the call sites
    """Gnani did not accept the session: a bad key, a plan without realtime, or a bad setting."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def normalise_language(value: Any) -> str:
    raw = str(value or "en-IN").strip().replace("_", "-")
    lang, _, region = raw.partition("-")
    code = f"{lang.lower()}-{(region or 'IN').upper()}"
    if code not in SUPPORTED_LANGUAGES:
        raise ValueError(f"language {raw!r} is not supported; use one of {', '.join(SUPPORTED_LANGUAGES)}")
    return code


def stream_url(base_url: str = DEFAULT_BASE_URL) -> str:
    """The WebSocket address for an API origin. Plain ``ws`` is allowed only for this machine."""
    parts = urlsplit((base_url or DEFAULT_BASE_URL).strip().rstrip("/"))
    host = parts.hostname or ""
    if parts.scheme in ("https", "wss"):
        scheme = "wss"
    elif parts.scheme in ("http", "ws") and host in _LOCAL_HOSTS:
        scheme = "ws"
    else:
        raise ValueError("the Gnani base URL must be https")
    path = parts.path.rstrip("/")
    for known in (STREAM_PATH, "/stt/v3"):
        if path.endswith(known):
            path = path[: -len(known)]
            break
    return f"{scheme}://{parts.netloc}{path}{STREAM_PATH}"


def handshake_headers(
    api_key: str,
    language: str,
    request_id: str,
    *,
    sample_rate: int = SAMPLE_RATE,
    itn: bool = False,
    native_numerals: bool = False,
) -> dict[str, str]:
    """The headers Gnani's SDK sends, in the same spelling."""
    headers = {
        "x-api-key-id": api_key,
        "lang_code": language,
        "x-sample-rate": str(sample_rate),
        "x-api-request-id": request_id,
    }
    if itn:
        headers["x-format"] = "transcribe"
        if native_numerals:
            headers["itn_native_numerals"] = "true"
    return headers


def ssl_context() -> ssl.SSLContext:
    """A verifying TLS context.

    Uses certifi's certificates when certifi is installed, because a fresh
    python.org install on macOS has no certificates of its own and every
    HTTPS call fails with CERTIFICATE_VERIFY_FAILED. Verification is never
    turned off.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Framer:
    """Turn PCM chunks of any size into exact 1024-byte frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        frames = []
        while len(self._buffer) >= FRAME_BYTES:
            frames.append(bytes(self._buffer[:FRAME_BYTES]))
            del self._buffer[:FRAME_BYTES]
        return frames

    def flush(self) -> list[bytes]:
        """The leftover samples, padded with silence to a whole frame."""
        if not self._buffer:
            return []
        frame = bytes(self._buffer) + b"\x00" * (FRAME_BYTES - len(self._buffer))
        self._buffer.clear()
        return [frame]


def frames_from_pcm(pcm: bytes) -> list[bytes]:
    framer = Framer()
    return framer.feed(pcm) + framer.flush()


class RealtimeSession:
    """One open realtime session. Create it with :meth:`open`."""

    def __init__(self, ws: Any, language: str, request_id: str, connected: dict[str, Any]) -> None:
        self._ws = ws
        self.language = language
        self.request_id = request_id
        self.connected = connected
        self.segments: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.frames_sent = 0
        self.closed_reason = ""
        self.flush_timed_out = False  # finish() gave up while a segment was still being transcribed
        self._closing = False
        self._pending = False
        self._last_event_at = asyncio.get_running_loop().time()
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read(), name="gnani-realtime-reader")

    # -------------------------------------------------------------------- open

    @classmethod
    async def open(
        cls,
        api_key: str,
        language: str = "en-IN",
        *,
        base_url: str = DEFAULT_BASE_URL,
        itn: bool = False,
        native_numerals: bool = False,
        open_timeout: float = 10.0,
    ) -> RealtimeSession:
        """Connect and wait for Gnani's ``connected`` message.

        Raises :class:`RealtimeRefused` when Gnani turns the session down and
        :class:`RealtimeError` when it cannot be reached. Neither message
        contains the key.
        """
        api_key = str(api_key or "").strip()
        if not api_key:
            raise RealtimeRefused("no Gnani API key was given")
        language = normalise_language(language)
        url = stream_url(base_url)
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        headers = handshake_headers(api_key, language, request_id, itn=itn, native_numerals=native_numerals)
        options: dict[str, Any] = {}
        if url.startswith("wss://"):
            options["ssl"] = ssl_context()
        if urlsplit(url).hostname in _LOCAL_HOSTS:
            options["proxy"] = None  # never send a local test connection through a proxy

        try:
            ws = await _ConnectWithoutRedirects(
                url,
                additional_headers=headers,
                open_timeout=open_timeout,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=1 << 20,
                **options,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            # The body says why. Should a service ever echo the key back, it is not passed on.
            body = (exc.response.body or b"").decode("utf-8", "replace").replace(api_key, "[key]").strip()[:200]
            raise RealtimeRefused(
                f"Gnani refused the realtime connection (HTTP {status}) {body}".strip(), status
            ) from exc
        except (InvalidHandshake, OSError, TimeoutError) as exc:
            raise RealtimeError(f"could not reach Gnani's realtime endpoint: {type(exc).__name__}: {exc}") from exc

        try:
            first = await asyncio.wait_for(ws.recv(), timeout=open_timeout)
        except ConnectionClosed as exc:
            detail = f"code {exc.rcvd.code} {exc.rcvd.reason}".strip() if exc.rcvd else "no reason given"
            raise RealtimeRefused(f"Gnani closed the realtime connection before it started ({detail})") from exc
        except TimeoutError as exc:
            await ws.close()
            raise RealtimeError("Gnani accepted the connection but never said it was ready") from exc
        try:
            event = json.loads(first) if isinstance(first, str) else {}
        except ValueError:
            event = {}
        if not isinstance(event, dict) or event.get("type") != "connected":
            await ws.close()
            message = str(event.get("message") or first) if isinstance(event, dict) else str(first)
            message = message.replace(api_key, "[key]")[:200]
            raise RealtimeRefused(f"Gnani did not start the realtime session: {message}")
        return cls(ws, language, request_id, event)

    # -------------------------------------------------------------------- send

    async def send_frame(self, frame: bytes) -> None:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"a frame must be exactly {FRAME_BYTES} bytes, got {len(frame)}")
        try:
            await self._ws.send(frame)
        except ConnectionClosed as exc:
            raise RealtimeError(f"the realtime connection closed: {self.closed_reason or exc}") from exc
        self.frames_sent += 1

    async def send_paced(self, frames: Iterable[bytes], pace: float = FRAME_SECONDS) -> None:
        """Send frames one every ``pace`` seconds, on a fixed schedule so the pace does not drift."""
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        for frame in frames:
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            await self.send_frame(frame)
            next_at += pace

    async def finish(
        self,
        *,
        flush_seconds: float = 1.2,
        settle_seconds: float = 0.6,
        timeout: float = 12.0,
        pace: float = FRAME_SECONDS,
    ) -> None:
        """Bring in the last segment, then close.

        Gnani ends a segment when it hears silence, measured in the audio it
        receives. So this sends silence, at speaking speed, until at least
        ``flush_seconds`` have gone by, nothing is being transcribed, and the
        service has said nothing for ``settle_seconds``. It gives up after
        ``timeout`` seconds, even when a send is stuck on a dead network, and
        sets ``flush_timed_out`` if a segment was still on its way.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        next_at = started
        try:
            async with asyncio.timeout(timeout):
                while not self._reader.done():
                    now = loop.time()
                    quiet = now - self._last_event_at
                    if now - started >= flush_seconds and not self._pending and quiet >= settle_seconds:
                        break
                    try:
                        await self.send_frame(SILENT_FRAME)
                    except RealtimeError:
                        break
                    next_at += pace
                    await asyncio.sleep(max(0.0, next_at - loop.time()))
        except TimeoutError:
            self.flush_timed_out = self._pending
        await self.close()

    async def close(self) -> None:
        self._closing = True
        try:
            await self._ws.close()
        finally:
            if not self._reader.done():
                await asyncio.wait({self._reader}, timeout=5)

    # ----------------------------------------------------------------- receive

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Every message from Gnani, as it arrives, until the connection closes."""
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    @property
    def transcript(self) -> str:
        return " ".join(segment["text"] for segment in self.segments if segment["text"]).strip()

    @property
    def audio_seconds(self) -> float:
        return round(self.frames_sent * FRAME_SECONDS, 2)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self._note(event)
                    await self._events.put(event)
        except ConnectionClosed as exc:
            if exc.rcvd and exc.rcvd.code not in (1000, 1001):
                self.closed_reason = f"closed by Gnani (code {exc.rcvd.code} {exc.rcvd.reason})".strip()
            elif not exc.rcvd and not self._closing:
                self.closed_reason = "the connection to Gnani was lost"
        finally:
            self._pending = False
            await self._events.put(None)

    def _note(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        self._last_event_at = asyncio.get_running_loop().time()
        if kind in _SEGMENT_CLOSING:
            self._pending = True
        elif kind == "transcript":
            self._pending = False
            text = str(event.get("text") or "").strip()
            self.segments.append(
                {
                    "index": len(self.segments),
                    "text": text,
                    "audio_duration_ms": event.get("audio_duration_ms"),
                    "latency_ms": event.get("latency"),
                    "segment_id": event.get("segment_id"),
                }
            )
        elif kind == "error":
            self._pending = False
            self.errors.append(str(event.get("message") or "unknown error")[:300])
