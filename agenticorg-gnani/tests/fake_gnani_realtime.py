"""A stand-in for Gnani's realtime speech-to-text server, for tests.

It follows the documented protocol (https://docs.gnani.ai/api/STT/stt-websocket):
it reads the handshake headers, says ``connected``, takes binary PCM frames,
and sends ``processing`` then ``transcript`` each time speech is followed by
enough silence. "Speech" here is any frame that is not all zeros (or, with
``threshold`` set, any frame with a sample louder than it, for real microphones).

How the real service turns down a bad key is not documented, so ``refuse``
chooses between the plausible ways: an HTTP 401 on the handshake (``echo``:
with the offered key repeated in the body), an ``error`` message in place of
``connected``, or closing straight away.
"""

from __future__ import annotations

import array
import json
import sys
import time
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

KEY = "realtime-test-key"


class FakeGnaniRealtime:
    def __init__(
        self,
        *,
        key: str | None = KEY,
        texts: list[str] | None = None,
        silence_frames: int = 4,
        refuse: str = "http",
        close_after_frames: int = 0,
        threshold: int = 0,
        error_at_frame: int = 0,
    ) -> None:
        self.key = key  # None: a broken service that lets any key in
        self.texts = list(texts or ["first segment", "second segment", "third segment"])
        self.silence_frames = silence_frames
        self.refuse = refuse  # "http" | "echo" | "error" | "close": how a wrong key is turned down
        self.close_after_frames = close_after_frames
        self.threshold = threshold
        self.error_at_frame = error_at_frame  # send an ``error`` message when this many frames have arrived
        self.handshakes: list[dict[str, str]] = []
        self.paths: list[str] = []
        self.frames: list[bytes] = []
        self.frame_times: list[float] = []
        self.text_messages: list[str] = []
        self.sessions = 0
        self._server: Any = None

    async def __aenter__(self) -> FakeGnaniRealtime:
        self._server = await serve(self._handle, "127.0.0.1", 0, process_request=self._gate)
        self.port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    def _gate(self, connection: Any, request: Any) -> Any:
        headers = {name.lower(): value for name, value in request.headers.raw_items()}
        self.handshakes.append(headers)
        self.paths.append(request.path)
        if request.path != "/stt/v3/stream":
            return connection.respond(HTTPStatus.NOT_FOUND, "Not Found\n")
        if not self._key_ok(headers) and self.refuse == "http":
            return connection.respond(HTTPStatus.UNAUTHORIZED, '{"detail": "Invalid API key"}\n')
        if not self._key_ok(headers) and self.refuse == "echo":
            offered = headers.get("x-api-key-id", "")
            return connection.respond(HTTPStatus.UNAUTHORIZED, f'{{"detail": "Invalid API key {offered}"}}\n')
        return None

    def _is_speech(self, frame: bytes) -> bool:
        if not self.threshold:
            return any(frame)
        samples = array.array("h", frame[: len(frame) - len(frame) % 2])
        if sys.byteorder == "big":
            samples.byteswap()
        return max(map(abs, samples), default=0) > self.threshold

    def _key_ok(self, headers: dict[str, str]) -> bool:
        return self.key is None or headers.get("x-api-key-id") == self.key

    async def _handle(self, ws: Any) -> None:
        headers = self.handshakes[-1]
        if not self._key_ok(headers):
            if self.refuse == "error":
                await ws.send(json.dumps({"type": "error", "message": "Invalid API key", "timestamp": "t"}))
            await ws.close(code=1008, reason="unauthorized")
            return
        self.sessions += 1
        await ws.send(
            json.dumps(
                {
                    "type": "connected",
                    "message": "STT service ready — VAD service connected",
                    "timestamp": "2026-01-01T00:00:00.000Z",
                    "config": {"sample_rate": int(headers.get("x-sample-rate", "16000")), "chunk_size": 512},
                }
            )
        )
        speech = silence = index = received = 0
        try:
            async for message in ws:
                if isinstance(message, str):
                    self.text_messages.append(message)
                    continue
                self.frames.append(message)
                self.frame_times.append(time.monotonic())
                received += 1
                if received == self.error_at_frame:
                    await ws.send(json.dumps({"type": "error", "message": "segment could not be transcribed"}))
                if self.close_after_frames and received >= self.close_after_frames:
                    await ws.close(code=1011, reason="server error")
                    return
                if self._is_speech(message):
                    speech += 1
                    silence = 0
                    continue
                silence += 1
                if speech and silence >= self.silence_frames:
                    await ws.send(json.dumps({"type": "processing", "timestamp": "t"}))
                    text = self.texts[index] if index < len(self.texts) else f"segment {index + 1}"
                    await ws.send(
                        json.dumps(
                            {
                                "type": "transcript",
                                "timestamp": "t",
                                "text": text,
                                "audio_duration_ms": speech * 32,
                                "segment_id": f"seg-{index}",
                                "segment_index": index,
                                "latency": 120,
                            }
                        )
                    )
                    index += 1
                    speech = 0
        except ConnectionClosed:
            pass


def speech_frames(count: int) -> list[bytes]:
    """Frames the fake server counts as speech."""
    return [b"\x10\x00" * 512 for _ in range(count)]
