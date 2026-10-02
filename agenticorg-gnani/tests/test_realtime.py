"""The realtime client against a stand-in Gnani server (tests/fake_gnani_realtime.py). No network, no key."""

from __future__ import annotations

import asyncio
import ssl
from http import HTTPStatus

import pytest
from fake_gnani_realtime import KEY, FakeGnaniRealtime, speech_frames
from websockets.asyncio.server import serve

from agenticorg_gnani.realtime import (
    FRAME_BYTES,
    SILENT_FRAME,
    Framer,
    RealtimeError,
    RealtimeRefused,
    RealtimeSession,
    frames_from_pcm,
    handshake_headers,
    ssl_context,
    stream_url,
)

FAST = {"flush_seconds": 0.05, "settle_seconds": 0.05, "timeout": 3.0, "pace": 0.002}


async def collect(session: RealtimeSession) -> list[dict]:
    return [event async for event in session.stream()]


# ------------------------------------------------------------------ handshake


async def test_handshake_matches_gnanis_sdk() -> None:
    async with FakeGnaniRealtime() as gnani:
        session = await RealtimeSession.open(KEY, "hi", base_url=gnani.base_url)
        await session.close()
    (headers,) = gnani.handshakes
    assert gnani.paths == ["/stt/v3/stream"]
    assert headers["x-api-key-id"] == KEY
    assert headers["lang_code"] == "hi-IN"
    assert headers["x-sample-rate"] == "16000"
    assert headers["x-api-request-id"] == session.request_id and session.request_id.startswith("req_")
    assert "x-format" not in headers and "itn_native_numerals" not in headers
    assert session.connected["config"] == {"sample_rate": 16000, "chunk_size": 512}


def test_headers_for_number_formatting() -> None:
    plain = handshake_headers("k", "en-IN", "req_1")
    assert plain == {"x-api-key-id": "k", "lang_code": "en-IN", "x-sample-rate": "16000", "x-api-request-id": "req_1"}
    itn = handshake_headers("k", "hi-IN", "req_1", itn=True, native_numerals=True)
    assert itn["x-format"] == "transcribe" and itn["itn_native_numerals"] == "true"


@pytest.mark.parametrize(
    ("base", "url"),
    [
        ("https://api.vachana.ai", "wss://api.vachana.ai/stt/v3/stream"),
        ("https://api.vachana.ai/", "wss://api.vachana.ai/stt/v3/stream"),
        ("https://api.vachana.ai/stt/v3", "wss://api.vachana.ai/stt/v3/stream"),
        ("wss://api.vachana.ai/stt/v3/stream", "wss://api.vachana.ai/stt/v3/stream"),
        ("http://127.0.0.1:9000", "ws://127.0.0.1:9000/stt/v3/stream"),
    ],
)
def test_stream_url(base: str, url: str) -> None:
    assert stream_url(base) == url


@pytest.mark.parametrize("base", ["http://api.vachana.ai", "ws://example.com", "ftp://x"])
def test_plain_connections_are_only_allowed_to_this_machine(base: str) -> None:
    with pytest.raises(ValueError, match="must be https"):
        stream_url(base)


def test_tls_always_verifies() -> None:
    context = ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True


async def test_unsupported_language_fails_before_connecting() -> None:
    async with FakeGnaniRealtime() as gnani:
        with pytest.raises(ValueError, match="not supported"):
            await RealtimeSession.open(KEY, "fr-FR", base_url=gnani.base_url)
    assert gnani.handshakes == []


# --------------------------------------------------------------------- refusal


@pytest.mark.parametrize("how", ["http", "error", "close"])
async def test_wrong_key_is_refused_and_never_echoed(how: str) -> None:
    async with FakeGnaniRealtime(refuse=how) as gnani:
        with pytest.raises(RealtimeRefused) as refused:
            await RealtimeSession.open("wrong-key-000", "en-IN", base_url=gnani.base_url)
    assert "wrong-key-000" not in str(refused.value)
    assert gnani.sessions == 0
    if how == "http":
        assert refused.value.status == 401 and "Invalid API key" in str(refused.value)
    if how == "error":
        assert "Invalid API key" in str(refused.value)


async def test_a_refusal_that_repeats_the_key_is_not_passed_on() -> None:
    async with FakeGnaniRealtime(refuse="echo") as gnani:
        with pytest.raises(RealtimeRefused) as refused:
            await RealtimeSession.open("wrong-key-000", "en-IN", base_url=gnani.base_url)
    assert "wrong-key-000" not in str(refused.value) and "Invalid API key [key]" in str(refused.value)


async def test_a_redirect_is_not_followed_so_the_key_goes_nowhere_else() -> None:
    async with FakeGnaniRealtime() as elsewhere:

        def redirect(connection, _request):
            response = connection.respond(HTTPStatus.FOUND, "moved\n")
            response.headers["Location"] = f"ws://127.0.0.1:{elsewhere.port}/stt/v3/stream"
            return response

        async with serve(lambda ws: ws.wait_closed(), "127.0.0.1", 0, process_request=redirect) as front:
            port = front.sockets[0].getsockname()[1]
            with pytest.raises(RealtimeRefused) as refused:
                await RealtimeSession.open(KEY, "en-IN", base_url=f"http://127.0.0.1:{port}")
    assert refused.value.status == 302
    assert elsewhere.handshakes == [], "the key was not offered to the address the redirect named"


async def test_missing_key_and_unreachable_server() -> None:
    with pytest.raises(RealtimeRefused, match="no Gnani API key"):
        await RealtimeSession.open("  ", "en-IN")
    async with FakeGnaniRealtime() as gnani:
        dead = gnani.base_url
    with pytest.raises(RealtimeError, match="could not reach"):
        await RealtimeSession.open(KEY, "en-IN", base_url=dead, open_timeout=2)


# ----------------------------------------------------------------------- audio


def test_framer_makes_exact_frames() -> None:
    framer = Framer()
    assert framer.feed(b"\x01" * 1000) == []
    frames = framer.feed(b"\x02" * 1100)
    assert [len(f) for f in frames] == [1024, 1024]
    (last,) = framer.flush()
    assert len(last) == FRAME_BYTES and last[:52] == b"\x02" * 52 and last[52:] == b"\x00" * 972
    assert framer.flush() == []
    assert [len(f) for f in frames_from_pcm(b"\x01" * 2500)] == [1024, 1024, 1024]


async def test_frames_must_be_exactly_1024_bytes() -> None:
    async with FakeGnaniRealtime() as gnani:
        session = await RealtimeSession.open(KEY, base_url=gnani.base_url)
        with pytest.raises(ValueError, match="exactly 1024"):
            await session.send_frame(b"\x00" * 1000)
        await session.close()
    assert gnani.frames == []


async def test_segments_arrive_while_speaking_and_finish_brings_in_the_last_one() -> None:
    async with FakeGnaniRealtime(texts=["मैं सीढ़ियों से गिर गई", "and broke my wrist"], silence_frames=4) as gnani:
        session = await RealtimeSession.open(KEY, "hi-IN", base_url=gnani.base_url)
        events = asyncio.create_task(collect(session))

        await session.send_paced(speech_frames(6) + [SILENT_FRAME] * 5, pace=0.002)  # a sentence, then a pause
        await session.send_paced(speech_frames(8), pace=0.002)  # still talking when they press stop
        await asyncio.sleep(0.05)
        assert [s["text"] for s in session.segments] == ["मैं सीढ़ियों से गिर गई"]

        await session.finish(**FAST)
        seen = await events

    assert [s["text"] for s in session.segments] == ["मैं सीढ़ियों से गिर गई", "and broke my wrist"]
    assert session.flush_timed_out is False and session.closed_reason == "" and session.errors == []
    assert session.transcript == "मैं सीढ़ियों से गिर गई and broke my wrist"
    assert session.segments[1] == {
        "index": 1,
        "text": "and broke my wrist",
        "audio_duration_ms": 8 * 32,
        "latency_ms": 120,
        "segment_id": "seg-1",
    }
    assert [e["type"] for e in seen] == ["processing", "transcript", "processing", "transcript"]
    assert all(len(frame) == FRAME_BYTES for frame in gnani.frames)
    assert gnani.frames[-1] == SILENT_FRAME and session.frames_sent == len(gnani.frames)
    assert gnani.text_messages == [], "nothing but audio is sent: Gnani has no end-of-audio message"


async def test_finish_stops_quickly_when_nothing_was_said() -> None:
    async with FakeGnaniRealtime() as gnani:
        session = await RealtimeSession.open(KEY, base_url=gnani.base_url)
        await session.finish(**FAST)
    assert session.segments == [] and session.transcript == ""
    assert 1 <= len(gnani.frames) < 200


async def test_finish_gives_up_after_the_timeout() -> None:
    async with FakeGnaniRealtime(silence_frames=10_000) as gnani:  # a server that never closes the segment
        session = await RealtimeSession.open(KEY, base_url=gnani.base_url)
        await session.send_paced(speech_frames(3), pace=0.002)
        session._pending = True  # as if "processing" had arrived and its transcript never did
        loop = asyncio.get_running_loop()
        started = loop.time()
        await session.finish(flush_seconds=0.05, settle_seconds=0.05, timeout=0.4, pace=0.002)
        assert 0.35 <= loop.time() - started < 2.0
    assert session.segments == [] and session.flush_timed_out is True


async def test_paced_sending_keeps_speaking_speed() -> None:
    async with FakeGnaniRealtime() as gnani:
        session = await RealtimeSession.open(KEY, base_url=gnani.base_url)
        await session.send_paced(speech_frames(16))  # default pace: one frame per 32 ms
        await session.close()
    elapsed = gnani.frame_times[-1] - gnani.frame_times[0]
    assert 0.40 <= elapsed <= 0.75, elapsed  # 15 gaps of 32 ms is 0.48 s


async def test_server_error_is_recorded_and_a_dropped_connection_is_reported() -> None:
    async with FakeGnaniRealtime(close_after_frames=3) as gnani:
        session = await RealtimeSession.open(KEY, base_url=gnani.base_url)
        with pytest.raises(RealtimeError, match="closed"):
            for frame in speech_frames(50):
                await session.send_frame(frame)
                await asyncio.sleep(0.005)
        await session.close()
    assert "1011" in session.closed_reason
