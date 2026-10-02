"""The live capture server, driven the way the browser page drives it, against a stand-in Gnani. No network, no key."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fake_gnani_realtime import KEY, FakeGnaniRealtime, speech_frames
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from agenticorg_gnani import voice_server
from agenticorg_gnani.realtime import FRAME_BYTES, SILENT_FRAME
from agenticorg_gnani.sessions import load_session
from agenticorg_gnani.voice_server import VoiceConfig, VoiceServer

FAST = {"flush_seconds": 0.05, "settle_seconds": 0.05, "timeout": 3.0, "pace": 0.002}
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@asynccontextmanager
async def running(tmp_path: Path, gnani: FakeGnaniRealtime, key: str = KEY, **overrides: Any):
    settings: dict[str, Any] = {"min_frame_gap": 0.001, "finish": FAST, **overrides}
    config = VoiceConfig(api_key=key, base_url=gnani.base_url, port=0, sessions_dir=tmp_path / "sessions", **settings)
    saved: list[tuple[dict[str, Any], Path]] = []
    server = VoiceServer(config, on_saved=lambda session, path: saved.append((session, path)))
    await server.start()
    try:
        yield server, saved
    finally:
        await server.stop()


def page_socket(server: VoiceServer, origin: str | None = "default"):
    if origin == "default":
        origin = f"http://localhost:{server.port}"
    return connect(f"ws://127.0.0.1:{server.port}/ws", origin=origin, proxy=None)


async def http_get(port: int, path: str) -> tuple[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.decode(), body.decode()


async def begin(ws: Any, language: str = "en-IN") -> dict[str, Any]:
    await ws.send(json.dumps({"type": "start", "language": language}))
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=5))


async def until(ws: Any, kind: str) -> list[dict[str, Any]]:
    """Messages from the server up to and including the first of this kind."""
    seen: list[dict[str, Any]] = []
    while True:
        seen.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=5)))
        if seen[-1]["type"] == kind:
            return seen


async def send_chunks(ws: Any, frames: list[bytes], size: int = 700) -> None:
    """Send audio the way a browser might: in pieces that do not line up with Gnani's frames."""
    data = b"".join(frames)
    for start in range(0, len(data), size):
        await ws.send(data[start : start + size])


async def wait_for(condition: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not condition():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.01)


# ------------------------------------------------------------------------ page


async def test_page_is_served_without_the_key(tmp_path: Path) -> None:
    async with FakeGnaniRealtime() as gnani, running(tmp_path, gnani) as (server, _):
        head, body = await http_get(server.port, "/")
        health_head, health = await http_get(server.port, "/healthz")
        missing_head, _ = await http_get(server.port, "/etc/passwd")
    assert head.startswith("HTTP/1.1 200")
    assert "text/html" in head and "no-store" in head and "X-Frame-Options: DENY" in head
    assert "Start recording" in body and "__SETTINGS__" not in body
    assert '"code": "hi-IN"' in body and '"maxSeconds": 300' in body
    assert KEY not in body and KEY not in head
    assert "http://" not in body.replace("http://localhost", "") and "https://" not in body, "the page loads nothing"
    assert health_head.startswith("HTTP/1.1 200") and json.loads(health) == {"ok": True, "active_sessions": 0}
    assert missing_head.startswith("HTTP/1.1 404")
    assert gnani.handshakes == [], "opening the page does not touch Gnani"


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://localhost:1", "http://evil.example:8765"])
async def test_only_the_capture_page_may_open_the_audio_socket(tmp_path: Path, origin: str | None) -> None:
    async with FakeGnaniRealtime() as gnani, running(tmp_path, gnani) as (server, _):
        with pytest.raises(InvalidStatus) as refused:
            async with page_socket(server, origin=origin):
                pass
        assert refused.value.response.status_code == 403
        async with page_socket(server, origin=f"http://127.0.0.1:{server.port}") as ws:
            assert (await begin(ws))["type"] == "ready"
    assert len(gnani.handshakes) == 1


def test_it_only_listens_on_this_machine_and_only_talks_https(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only listens on this machine"):
        VoiceConfig(api_key="k", host="0.0.0.0", sessions_dir=tmp_path)
    with pytest.raises(ValueError, match="must be https"):
        VoiceConfig(api_key="k", base_url="http://api.vachana.ai", sessions_dir=tmp_path)
    with pytest.raises(ValueError, match="max_seconds"):
        VoiceConfig(api_key="k", max_seconds=3600, sessions_dir=tmp_path)


# --------------------------------------------------------------------- session


async def test_a_statement_is_transcribed_live_and_saved(tmp_path: Path) -> None:
    texts = ["I slipped on the stairs on the third of March", "and was admitted to City Hospital"]
    async with FakeGnaniRealtime(texts=texts) as gnani, running(tmp_path, gnani) as (server, saved):
        async with page_socket(server) as ws:
            ready = await begin(ws, "hi-IN")
            assert ready["type"] == "ready" and ready["max_seconds"] == 300
            session_id = ready["session_id"]

            await send_chunks(ws, speech_frames(6) + [SILENT_FRAME] * 5)  # a sentence, then a pause
            live = await until(ws, "transcript")
            assert live[-1] == {"type": "transcript", "index": 0, "text": texts[0]}
            assert [m["type"] for m in live] == ["processing", "transcript"]

            await send_chunks(ws, speech_frames(8) + [b"\x10\x00" * 100])  # still talking, mid-frame, at "stop"
            await ws.send(json.dumps({"type": "stop"}))
            rest = await until(ws, "saved")

        assert rest[-1] == {
            "type": "saved",
            "session_id": session_id,
            "transcript": " ".join(texts),
            "segments": 2,
            "audio_seconds": round(20 * 0.032, 2),
            "complete": True,
        }
        assert {"type": "transcript", "index": 1, "text": texts[1]} in rest

    # what Gnani received: exact frames, the right language, the server's key
    assert all(len(frame) == FRAME_BYTES for frame in gnani.frames)
    assert gnani.frames[19][:200] == b"\x10\x00" * 100 and not any(gnani.frames[19][200:]), "last part-frame is padded"
    assert gnani.handshakes[0]["x-api-key-id"] == KEY and gnani.handshakes[0]["lang_code"] == "hi-IN"

    # what was saved
    ((session, path),) = saved
    directory = tmp_path / "sessions"
    assert path == directory / f"{session_id}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(directory.stat().st_mode) == 0o700
    on_disk = load_session(directory, session_id)
    assert on_disk == session
    assert on_disk["transcript"] == " ".join(texts) and on_disk["language_code"] == "hi-IN"
    assert on_disk["source"] == "gnani_realtime" and on_disk["ended"] == "stopped" and on_disk["complete"] is True
    assert [s["text"] for s in on_disk["segments"]] == texts
    assert "problems" not in on_disk and "audio_file" not in on_disk
    assert KEY not in path.read_text(encoding="utf-8")
    assert sorted(p.name for p in directory.iterdir()) == [f"{session_id}.json"], "no audio is kept by default"


async def test_audio_can_be_kept(tmp_path: Path) -> None:
    async with FakeGnaniRealtime() as gnani, running(tmp_path, gnani, save_audio=True) as (server, saved):
        async with page_socket(server) as ws:
            await begin(ws)
            await send_chunks(ws, speech_frames(5))
            await ws.send(json.dumps({"type": "stop"}))
            await until(ws, "saved")
    ((session, path),) = saved
    recording = path.with_suffix(".wav")
    assert session["audio_file"] == recording.name and stat.S_IMODE(recording.stat().st_mode) == 0o600
    with wave.open(str(recording), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        assert audio.getnframes() == 5 * 512


async def test_audio_reaches_gnani_no_faster_than_speech(tmp_path: Path) -> None:
    async with FakeGnaniRealtime() as gnani, running(tmp_path, gnani, min_frame_gap=0.02) as (server, _):
        async with page_socket(server) as ws:
            await begin(ws)
            await ws.send(b"".join(speech_frames(20)))  # a backlog arriving all at once
            await wait_for(lambda: len(gnani.frames) >= 20)
            assert gnani.frame_times[19] - gnani.frame_times[0] >= 19 * 0.02 * 0.9
            await ws.send(json.dumps({"type": "stop"}))
            await until(ws, "saved")


async def test_recording_ends_and_saves_at_the_time_limit(tmp_path: Path) -> None:
    async with FakeGnaniRealtime(texts=["cut off at the limit"]) as gnani, running(tmp_path, gnani) as (server, saved):
        server.config.max_seconds = 0.3
        async with page_socket(server) as ws:
            await begin(ws)
            talking = asyncio.create_task(send_chunks(ws, speech_frames(400), size=1024))  # never presses stop
            messages = await until(ws, "saved")
            talking.cancel()
    assert {"type": "stopping", "message": "Time limit reached. Saving what you said."} in messages
    ((session, _),) = saved
    assert session["ended"] == "time_limit" and session["complete"] is True
    assert session["transcript"] == "cut off at the limit"


async def test_closing_the_tab_still_saves_what_was_said(tmp_path: Path) -> None:
    async with FakeGnaniRealtime(texts=["said before the tab closed"]) as gnani:
        async with running(tmp_path, gnani) as (server, saved):
            async with page_socket(server) as ws:
                await begin(ws)
                await send_chunks(ws, speech_frames(6))
                await wait_for(lambda: len(gnani.frames) >= 6)
            await wait_for(lambda: saved)
    ((session, path),) = saved
    assert session["ended"] == "disconnected" and session["complete"] is False
    assert session["transcript"] == "said before the tab closed" and path.exists()


async def test_gnani_dropping_the_connection_saves_what_arrived_and_says_so(tmp_path: Path) -> None:
    async with FakeGnaniRealtime(texts=["the first sentence"], close_after_frames=30) as gnani:
        async with running(tmp_path, gnani) as (server, saved), page_socket(server) as ws:
            await begin(ws)
            await send_chunks(ws, speech_frames(6) + [SILENT_FRAME] * 6)
            await until(ws, "transcript")
            talking = asyncio.create_task(send_chunks(ws, speech_frames(400), size=1024))
            messages = await until(ws, "saved")
            talking.cancel()
    assert messages[-1]["complete"] is False and messages[-1]["transcript"] == "the first sentence"
    ((session, _),) = saved
    assert session["ended"] == "gnani_closed" and any("1011" in problem for problem in session["problems"])


async def test_a_backlog_gnani_never_takes_does_not_hang_the_server(tmp_path: Path) -> None:
    """Gnani goes away while more audio than the backlog holds is waiting. The page is told; nothing is left stuck."""
    async with FakeGnaniRealtime(close_after_frames=1) as gnani:
        async with running(tmp_path, gnani, min_frame_gap=0.3) as (server, saved), page_socket(server) as ws:
            await begin(ws)
            for _ in range(20):
                await ws.send(b"".join(speech_frames(30)))  # 600 frames: more than 15 seconds' worth
            await ws.send(json.dumps({"type": "stop"}))
            (message,) = await until(ws, "error")
            assert "1011" in message["message"] and "dropped" in message["message"]
            await wait_for(lambda: server._active == 0)
    assert saved == []


async def test_a_segment_gnani_could_not_transcribe_marks_the_session_incomplete(tmp_path: Path) -> None:
    async with FakeGnaniRealtime(texts=["the part that worked"], error_at_frame=3) as gnani:
        async with running(tmp_path, gnani) as (server, saved), page_socket(server) as ws:
            await begin(ws)
            await send_chunks(ws, speech_frames(8))
            await ws.send(json.dumps({"type": "stop"}))
            messages = await until(ws, "saved")
    assert messages[-1]["complete"] is False
    assert any(m["type"] == "notice" and "segment could not be transcribed" in m["message"] for m in messages)
    ((session, _),) = saved
    assert session["transcript"] == "the part that worked" and session["problems"] == [
        "segment could not be transcribed"
    ]


def test_the_key_is_not_shown_when_the_config_is_printed(tmp_path: Path) -> None:
    config = VoiceConfig(api_key="config-secret-key", sessions_dir=tmp_path)
    assert "config-secret-key" not in repr(config) and "config-secret-key" not in str(config)


async def test_silence_saves_nothing(tmp_path: Path) -> None:
    async with FakeGnaniRealtime() as gnani, running(tmp_path, gnani) as (server, saved):
        async with page_socket(server) as ws:
            await begin(ws)
            await send_chunks(ws, [SILENT_FRAME] * 10)
            await ws.send(json.dumps({"type": "stop"}))
            (message,) = await until(ws, "error")
    assert message["message"].startswith("No speech was recognised.")
    assert saved == [] and not (tmp_path / "sessions").exists()


# -------------------------------------------------------------------- refusals


@pytest.mark.parametrize("how", ["http", "error", "close"])
async def test_a_key_gnani_refuses_is_reported_to_the_page_without_the_key(tmp_path: Path, how: str) -> None:
    async with FakeGnaniRealtime(refuse=how) as gnani, running(tmp_path, gnani, key="wrong-key-000") as (server, saved):
        async with page_socket(server) as ws:
            message = await begin(ws)
    assert message["type"] == "error" and message["message"].startswith("Gnani did not accept the connection.")
    assert "wrong-key-000" not in json.dumps(message)
    assert saved == [] and gnani.sessions == 0


async def test_gnani_unreachable_is_reported(tmp_path: Path) -> None:
    async with FakeGnaniRealtime() as gnani:
        pass  # started and stopped: nothing is listening on its port now
    async with running(tmp_path, gnani) as (server, _), page_socket(server) as ws:
        message = await begin(ws)
    assert message["type"] == "error" and message["message"].startswith("Could not reach Gnani.")


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        (json.dumps({"type": "start", "language": "fr-FR"}), "not supported"),
        (json.dumps({"type": "hello"}), "did not start the recording properly"),
        ("not json", "did not start the recording properly"),
        (b"\x00" * 1024, "did not start the recording properly"),
    ],
)
async def test_a_page_that_starts_wrongly_is_told_so(tmp_path: Path, first: str | bytes, expected: str) -> None:
    async with FakeGnaniRealtime() as gnani, running(tmp_path, gnani) as (server, _):
        async with page_socket(server) as ws:
            await ws.send(first)
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert message["type"] == "error" and expected in message["message"]
    assert gnani.handshakes == []


async def test_recordings_at_once_are_limited(tmp_path: Path) -> None:
    async with FakeGnaniRealtime() as gnani, running(tmp_path, gnani, max_sessions=1) as (server, _):
        async with page_socket(server) as first, page_socket(server) as second:
            assert (await begin(first))["type"] == "ready"
            message = json.loads(await asyncio.wait_for(second.recv(), timeout=5))  # told before it says anything
            assert message["type"] == "error" and "already in progress" in message["message"]
            await send_chunks(first, speech_frames(4))
            await first.send(json.dumps({"type": "stop"}))
            await until(first, "saved")
        async with page_socket(server) as third:  # the slot is free again
            assert (await begin(third))["type"] == "ready"


# ------------------------------------------------------------------------- cli


def test_cli_needs_the_key_and_a_loopback_https_setup(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.delenv("GNANI_API_KEY", raising=False)
    assert voice_server.main([]) == 2
    assert "Set GNANI_API_KEY first" in capsys.readouterr().out
    monkeypatch.setenv("GNANI_API_KEY", "cli-secret-key")
    monkeypatch.setenv("GNANI_BASE_URL", "http://api.vachana.ai")
    assert voice_server.main(["--no-open"]) == 2
    output = capsys.readouterr().out
    assert "must be https" in output and "cli-secret-key" not in output


async def run_check(gnani: FakeGnaniRealtime, key: str, *args: str) -> tuple[int, str]:
    environment = {**os.environ, "GNANI_API_KEY": key, "GNANI_BASE_URL": gnani.base_url}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPTS / "check_realtime.py"),
        *args,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    return process.returncode or 0, output.decode()


def write_clip(path: Path, seconds: float = 0.4, rate: int = 44100, channels: int = 2) -> Path:
    with wave.open(str(path), "wb") as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(b"\x10\x20" * int(rate * seconds) * channels)
    return path


async def test_check_realtime_passes_with_a_good_key(tmp_path: Path) -> None:
    clip = write_clip(tmp_path / "clip.wav")
    async with FakeGnaniRealtime(texts=["hello from the clip"]) as gnani:
        code, output = await run_check(gnani, KEY, str(clip), "ta")
    assert code == 0, output
    assert "[PASS] your key is accepted for realtime" in output
    assert "[PASS] transcript -> hello from the clip" in output
    assert "[PASS] a wrong key is refused" in output and "All checks passed." in output
    assert KEY not in output
    assert gnani.handshakes[0]["lang_code"] == "ta-IN" and all(len(f) == FRAME_BYTES for f in gnani.frames)


async def test_check_realtime_still_checks_the_key_when_the_clip_is_not_a_wav(tmp_path: Path) -> None:
    clip = tmp_path / "voice.m4a"
    clip.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64)
    async with FakeGnaniRealtime() as gnani:
        code, output = await run_check(gnani, KEY, str(clip))
    assert code == 0, output
    assert "Leaving the clip out" in output and "[PASS] your key is accepted for realtime" in output
    assert "[PASS] audio frames accepted" in output


async def test_check_realtime_fails_with_a_refused_key() -> None:
    async with FakeGnaniRealtime() as gnani:
        code, output = await run_check(gnani, "some-other-key-123")
    assert code == 1 and "[FAIL] your key on the realtime endpoint" in output
    assert "some-other-key-123" not in output


async def test_check_realtime_does_not_trust_a_service_that_accepts_any_key() -> None:
    async with FakeGnaniRealtime(key=None) as gnani:
        code, output = await run_check(gnani, KEY)
    assert code == 1
    assert "[FAIL] Gnani opened a realtime session for a wrong key" in output
