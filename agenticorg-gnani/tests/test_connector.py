"""Tests for the Gnani STT connector.

Run inside AgenticOrg's environment (the connector imports its framework):

    cd <agentic-org checkout>
    pip install -e <this package> pytest pytest-asyncio
    python -m pytest <this package>/tests -p no:cacheprovider -o addopts=""

No network and no API key: Gnani and the audio host are an httpx MockTransport.
"""

from __future__ import annotations

import base64
import io
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agenticorg_gnani.connector import GnaniSttConnector
from agenticorg_gnani.sessions import save_session

KEY = "test-key-123"
OK_BODY = {"success": True, "request_id": "req_server", "timestamp": "20260101_000000.000", "transcript": "नमस्ते"}


def wav(seconds: float = 1.0, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x01\x00" * int(seconds * rate))
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Upstream:
    """Scriptable stand-in for api.vachana.ai and for an audio file host."""

    def __init__(self) -> None:
        self.stt_requests: list[httpx.Request] = []
        self.file_requests: list[httpx.Request] = []
        self.stt_replies: list[httpx.Response] = []
        self.files: dict[str, httpx.Response] = {}

    def reply(self, status: int = 200, json: Any = None, text: str | None = None) -> None:
        self.stt_replies.append(
            httpx.Response(status, text=text) if text is not None else httpx.Response(status, json=json or OK_BODY)
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.vachana.ai":
            request.read()
            self.stt_requests.append(request)
            if request.url.path != "/stt/v3":
                return httpx.Response(404, json={"detail": "Not Found"})
            if len(self.stt_replies) > 1:
                return self.stt_replies.pop(0)
            return self.stt_replies[0] if self.stt_replies else httpx.Response(200, json=OK_BODY)
        self.file_requests.append(request)
        return self.files.get(str(request.url), httpx.Response(404))


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Upstream:
    up = Upstream()
    monkeypatch.setattr(
        GnaniSttConnector,
        "_connector_transport_and_dns",
        lambda self: (httpx.MockTransport(up.handler), False),
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("agenticorg_gnani.connector.asyncio.sleep", no_sleep)
    return up


@pytest.fixture
async def connect(upstream: Upstream) -> Callable[..., Any]:
    opened: list[GnaniSttConnector] = []

    async def _connect(**config: Any) -> GnaniSttConnector:
        connector = GnaniSttConnector({"api_key": KEY, **config})
        await connector.connect()
        opened.append(connector)
        return connector

    yield _connect
    for connector in opened:
        await connector.disconnect()


def form(request: httpx.Request) -> bytes:
    return request.content


# ----------------------------------------------------------------- wire format


async def test_request_matches_gnani_sdk_wire_format(upstream: Upstream, connect) -> None:
    connector = await connect()
    result = await connector.execute_tool("query", {"audio_base64": b64(wav()), "language_code": "hi-IN"})

    (request,) = upstream.stt_requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.vachana.ai/stt/v3"
    assert request.headers["X-API-Key-ID"] == KEY
    assert request.headers["X-API-Request-ID"].startswith("req_")
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = form(request)
    assert b'name="audio_file"; filename="audio.wav"' in body
    assert b'name="language_code"\r\n\r\nhi-IN' in body
    assert b'name="format"\r\n\r\nverbatim' in body
    assert b"itn_native_numerals" not in body

    assert result == {
        "transcript": "नमस्ते",
        "language_code": "hi-IN",
        "format": "verbatim",
        "request_id": "req_server",
        "source": "gnani_rest",
        "audio_format": "wav",
        "audio_seconds": 1.0,
    }


async def test_itn_and_native_numerals(upstream: Upstream, connect) -> None:
    connector = await connect()
    await connector.transcribe_audio(audio_base64=b64(wav()), language_code="hi", itn="true", native_numerals=True)
    body = form(upstream.stt_requests[0])
    assert b'name="format"\r\n\r\ntranscribe' in body
    assert b'name="itn_native_numerals"\r\n\r\ntrue' in body
    assert b'name="language_code"\r\n\r\nhi-IN' in body


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.vachana.ai",
        "https://api.vachana.ai/",
        "https://api.vachana.ai/stt/v3",
        "https://api.vachana.ai/stt/v3/",
        "",
    ],
)
async def test_base_url_is_accepted_with_or_without_the_endpoint_path(upstream: Upstream, connect, base_url) -> None:
    connector = await connect(base_url=base_url)
    await connector.transcribe_audio(audio_base64=b64(wav()))
    assert str(upstream.stt_requests[0].url) == "https://api.vachana.ai/stt/v3"


async def test_other_connectors_merged_config_cannot_redirect_the_key(upstream: Upstream) -> None:
    """AgenticOrg merges every linked connector's config into one dict."""
    merged = {
        "base_url": "https://gmail.googleapis.com",  # another connector's
        "api_key": "SG.someone-elses-key",  # another connector's
        "gnani_api_key": KEY,
    }
    connector = GnaniSttConnector(merged)
    await connector.connect()
    await connector.transcribe_audio(audio_base64=b64(wav()))
    assert (await connector.health_check())["status"] == "healthy"
    await connector.disconnect()

    assert {r.url.host for r in upstream.stt_requests} == {"api.vachana.ai"}
    assert {r.headers["X-API-Key-ID"] for r in upstream.stt_requests} == {KEY}
    assert upstream.file_requests == []


async def test_gnani_api_key_alone_counts_as_configured(upstream: Upstream) -> None:
    connector = GnaniSttConnector({"gnani_api_key": KEY})
    await connector.connect()
    assert (await connector.health_check())["status"] == "healthy"
    await connector.disconnect()


def test_base_url_override_rules() -> None:
    assert GnaniSttConnector({"base_url": "https://eu.vachana.ai/stt/v3"}).base_url == "https://eu.vachana.ai"
    assert GnaniSttConnector({"base_url": "https://evil.example/vachana.ai"}).base_url == "https://api.vachana.ai"
    assert GnaniSttConnector({"base_url": None}).base_url == "https://api.vachana.ai"
    custom = {"base_url": "https://api.vachana.ai", "gnani_base_url": "https://stt.acme.example/"}
    assert GnaniSttConnector(custom).base_url == "https://stt.acme.example"


# ---------------------------------------------------------------- audio input


async def test_data_uri_and_format_detection(upstream: Upstream, connect) -> None:
    connector = await connect()
    ogg = b"OggS" + b"\x00" * 64
    result = await connector.transcribe_audio(audio_base64="data:audio/ogg;codecs=opus;base64," + b64(ogg))
    assert result["audio_format"] == "ogg"
    assert b'filename="audio.ogg"' in form(upstream.stt_requests[0])
    assert b"Content-Type: audio/ogg" in form(upstream.stt_requests[0])


async def test_audio_url_is_downloaded_without_the_api_key(upstream: Upstream, connect) -> None:
    upstream.files["https://files.example.com/a/note"] = httpx.Response(
        302, headers={"location": "https://cdn.example.com/note.mp3?sig=abc"}
    )
    upstream.files["https://cdn.example.com/note.mp3?sig=abc"] = httpx.Response(
        200, content=b"ID3" + b"\x00" * 100, headers={"content-type": "audio/mpeg"}
    )
    connector = await connect()
    result = await connector.transcribe_audio(audio_url="https://files.example.com/a/note", language_code="en-IN")

    assert result["audio_format"] == "mp3"
    assert [str(r.url) for r in upstream.file_requests] == [
        "https://files.example.com/a/note",
        "https://cdn.example.com/note.mp3?sig=abc",
    ]
    assert all("x-api-key-id" not in r.headers for r in upstream.file_requests)
    assert upstream.stt_requests[0].headers["X-API-Key-ID"] == KEY


@pytest.mark.parametrize(
    "url",
    [
        "http://files.example.com/a.wav",
        "https://localhost/a.wav",
        "https://127.0.0.1/a.wav",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
async def test_audio_url_must_be_public_https(upstream: Upstream, connect, url: str) -> None:
    connector = await connect()
    with pytest.raises(ValueError, match="public https URL"):
        await connector.transcribe_audio(audio_url=url)
    assert upstream.file_requests == [] and upstream.stt_requests == []


async def test_redirect_to_a_private_host_is_refused(upstream: Upstream, connect) -> None:
    upstream.files["https://files.example.com/a.wav"] = httpx.Response(
        302, headers={"location": "https://localhost/internal"}
    )
    connector = await connect()
    with pytest.raises(ValueError, match="public https URL"):
        await connector.transcribe_audio(audio_url="https://files.example.com/a.wav")
    assert len(upstream.file_requests) == 1 and upstream.stt_requests == []


async def test_download_errors_and_size_cap(upstream: Upstream, connect, monkeypatch) -> None:
    connector = await connect()
    with pytest.raises(ValueError, match="HTTP 404 from files.example.com"):
        await connector.transcribe_audio(audio_url="https://files.example.com/missing.wav?token=secret")

    monkeypatch.setattr("agenticorg_gnani.connector.MAX_AUDIO_BYTES", 1024)
    upstream.files["https://files.example.com/big.wav"] = httpx.Response(200, content=wav(2))
    with pytest.raises(ValueError, match="larger than"):
        await connector.transcribe_audio(audio_url="https://files.example.com/big.wav")
    with pytest.raises(ValueError, match="larger than"):
        await connector.transcribe_audio(audio_base64=b64(wav(2)))
    assert upstream.stt_requests == []


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "exactly one of"),
        ({"audio_url": "https://files.example.com/a.wav", "audio_base64": "AAAA"}, "exactly one of"),
        ({"audio_base64": "not base64!!"}, "not valid base64"),
        ({"audio_base64": b64(b"\x00" * 40)}, "audio_format must be one of"),
        ({"audio_base64": b64(wav()), "audio_format": "wma"}, "audio_format must be one of"),
        ({"audio_base64": b64(wav()), "language_code": "fr-FR"}, "not supported"),
        ({"audio_base64": b64(wav(61))}, "at most 60 s"),
    ],
)
async def test_bad_input_is_rejected_before_calling_gnani(upstream: Upstream, connect, params, message) -> None:
    connector = await connect()
    with pytest.raises(ValueError, match=message):
        await connector.execute_tool("query", params)
    assert upstream.stt_requests == []


# -------------------------------------------------------------- live sessions

STATEMENT = "I slipped on the stairs on the third of March and was admitted to City Hospital"


def saved_session(directory: Path, **changes: Any) -> str:
    session = {
        "session_id": "vs_0123456789abcdef",
        "source": "gnani_realtime",
        "language_code": "hi-IN",
        "format": "verbatim",
        "complete": True,
        "audio_seconds": 6.4,
        "request_id": "req_live",
        "segments": [{"index": 0, "text": STATEMENT}],
        "transcript": STATEMENT,
        **changes,
    }
    save_session(directory, session)
    return session["session_id"]


async def test_a_live_voice_session_is_read_from_the_capture_folder(upstream: Upstream, connect, tmp_path) -> None:
    session_id = saved_session(tmp_path)
    connector = await connect(gnani_sessions_dir=str(tmp_path))
    result = await connector.execute_tool("query", {"voice_session_id": session_id, "language_code": "en-IN"})
    assert result == {
        "transcript": STATEMENT,
        "language_code": "hi-IN",  # the language chosen on the capture page, not the caller's guess
        "format": "verbatim",
        "source": "gnani_realtime",
        "voice_session_id": session_id,
        "read_from": "capture_folder",
        "request_id": "req_live",
        "audio_seconds": 6.4,
    }
    assert upstream.stt_requests == [], "it was transcribed while it was spoken; Gnani is not called again"


async def test_the_capture_folder_can_come_from_the_environment(upstream: Upstream, connect, tmp_path, monkeypatch):
    session_id = saved_session(tmp_path, complete=False)
    monkeypatch.setenv("GNANI_SESSIONS_DIR", str(tmp_path))
    result = await (await connect()).execute_tool("query", {"voice_session_id": session_id})
    assert result["transcript"] == STATEMENT
    assert result["note"] == "The recording ended early, so the statement may be cut short."


async def test_a_transcript_sent_with_the_run_needs_no_folder(upstream: Upstream, connect, tmp_path) -> None:
    connector = await connect(gnani_sessions_dir=str(tmp_path / "not-there"))
    result = await connector.execute_tool(
        "query",
        {"voice_session_id": "vs_0123456789abcdef", "voice_transcript": f"  {STATEMENT} ", "language_code": "ta"},
    )
    assert result == {
        "transcript": STATEMENT,
        "language_code": "ta-IN",
        "format": "verbatim",
        "source": "gnani_realtime",
        "voice_session_id": "vs_0123456789abcdef",
        "read_from": "run",
    }
    assert upstream.stt_requests == []


async def test_a_statement_that_starts_with_a_dollar_sign_is_kept(upstream: Upstream, connect) -> None:
    said = "$500 was the bill, and they asked for $200 more"
    connector = await connect()
    result = await connector.execute_tool(
        "query", {"voice_session_id": "vs_0123456789abcdef", "voice_transcript": said}
    )
    assert result["transcript"] == said


async def test_a_voice_session_needs_no_gnani_key(tmp_path) -> None:
    session_id = saved_session(tmp_path)
    connector = GnaniSttConnector({"gnani_sessions_dir": str(tmp_path)})
    assert (await connector.transcribe_audio(voice_session_id=session_id))["transcript"] == STATEMENT


async def test_placeholders_a_run_did_not_fill_count_as_left_out(upstream: Upstream, connect, tmp_path) -> None:
    """A workflow step sends "$audio_url" as it is when the run has no audio_url."""
    session_id = saved_session(tmp_path)
    connector = await connect(gnani_sessions_dir=str(tmp_path))
    unfilled = {
        "audio_url": "$audio_url",
        "voice_session_id": "$voice_session_id",
        "voice_transcript": "$voice_transcript",
        "language_code": "$language_code",
    }
    from_session = await connector.execute_tool("query", {**unfilled, "voice_session_id": session_id})
    assert from_session["read_from"] == "capture_folder"
    assert upstream.stt_requests == [] and upstream.file_requests == []

    upstream.files["https://files.example.com/note.wav"] = httpx.Response(200, content=wav())
    from_clip = await connector.execute_tool("query", {**unfilled, "audio_url": "https://files.example.com/note.wav"})
    assert from_clip["source"] == "gnani_rest" and from_clip["language_code"] == "en-IN"

    with pytest.raises(ValueError, match="exactly one of voice_session_id, audio_url or audio_base64"):
        await connector.execute_tool("query", unfilled)


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"voice_session_id": "vs_ffffffffffffffff"}, "No voice session vs_ffffffffffffffff"),
        ({"voice_session_id": "../../etc/passwd"}, "voice_session_id must be"),
        ({"voice_session_id": "vs_0123456789abcdef", "audio_url": "https://files.example.com/a.wav"}, "not both"),
        ({"voice_transcript": "hello", "audio_base64": "AAAA"}, "not both"),
        ({"voice_transcript": "hello"}, "needs the voice_session_id"),
        ({"voice_session_id": "vs_0123456789abcdef", "voice_transcript": "x" * 20_001}, "longer than 20000"),
        ({"voice_session_id": "vs_empty"}, "has no transcript"),
        ({"voice_session_id": "vs_foreign"}, "not a voice session written by the capture server"),
        ({"voice_session_id": "vs_broken"}, "could not be read"),
        ({"voice_session_id": "vs_0123456789abcdef", "language_code": "fr-FR"}, "not supported"),
    ],
)
async def test_bad_voice_sessions_are_refused(upstream: Upstream, connect, tmp_path, params, message) -> None:
    saved_session(tmp_path)
    saved_session(tmp_path, session_id="vs_empty", transcript="  ")
    saved_session(tmp_path, session_id="vs_foreign", source="someone_else")
    (tmp_path / "vs_broken.json").write_text("{not json", encoding="utf-8")
    connector = await connect(gnani_sessions_dir=str(tmp_path))
    with pytest.raises(ValueError, match=message):
        await connector.execute_tool("query", params)
    assert upstream.stt_requests == []


# --------------------------------------------------------------------- errors


async def test_rejected_key_raises_http_status_error(upstream: Upstream, connect) -> None:
    upstream.reply(401, json={"detail": "Invalid API key"})
    connector = await connect()
    with pytest.raises(httpx.HTTPStatusError) as err:
        await connector.transcribe_audio(audio_base64=b64(wav()))
    assert err.value.response.status_code == 401
    assert len(upstream.stt_requests) == 1


async def test_transient_failure_is_retried_once(upstream: Upstream, connect) -> None:
    upstream.reply(503, text="busy")
    upstream.reply(200)
    connector = await connect()
    result = await connector.transcribe_audio(audio_base64=b64(wav()))
    assert result["transcript"] == "नमस्ते"
    first, second = upstream.stt_requests
    assert first.headers["X-API-Request-ID"] == second.headers["X-API-Request-ID"]


async def test_success_false_is_an_error(upstream: Upstream, connect) -> None:
    upstream.reply(200, json={"success": False, "message": "no speech detected"})
    connector = await connect()
    with pytest.raises(ValueError, match="no speech detected"):
        await connector.transcribe_audio(audio_base64=b64(wav()))


# --------------------------------------------------------------------- health


async def test_health_not_configured_without_key() -> None:
    assert (await GnaniSttConnector({}).health_check())["status"] == "not_configured"
    connector = GnaniSttConnector({"api_key": KEY})
    assert (await connector.health_check())["status"] == "not_connected"


@pytest.mark.parametrize(
    ("replies", "status", "calls"),
    [
        ([(200, OK_BODY)], "healthy", 1),
        ([(401, {"detail": "Unauthorized"})], "unhealthy", 1),
        ([(403, {"detail": "Forbidden"})], "unhealthy", 1),
        ([(400, {"detail": "Invalid API key"})], "unhealthy", 1),
        ([(400, {"detail": "No speech found in audio"})], "healthy", 1),
        ([(422, {"detail": "audio too short"})], "healthy", 1),
        ([(429, {"detail": "slow down"})], "unhealthy", 1),
        ([(500, {"detail": "boom"}), (422, {"detail": "audio_file required"})], "healthy", 2),
        ([(500, {"detail": "boom"}), (401, {"detail": "Unauthorized"})], "unhealthy", 2),
        ([(500, {"detail": "boom"}), (503, {"detail": "down"})], "unhealthy", 2),
    ],
)
async def test_health_check_outcomes(upstream: Upstream, connect, replies, status, calls) -> None:
    for code, body in replies:
        upstream.reply(code, json=body)
    connector = await connect()
    health = await connector.health_check()
    assert health["status"] == status, health
    assert len(upstream.stt_requests) == calls
    probe = upstream.stt_requests[0]
    assert probe.headers["X-API-Key-ID"] == KEY
    assert b'name="audio_file"; filename="probe.wav"' in form(probe)


async def test_health_reports_a_wrong_base_url(upstream: Upstream, monkeypatch) -> None:
    connector = GnaniSttConnector({"api_key": KEY, "base_url": "https://api.vachana.ai/v9"})
    await connector.connect()
    health = await connector.health_check()
    await connector.disconnect()
    assert health["status"] == "unhealthy" and "base_url" in health["reason"]


# ------------------------------------------------- inside AgenticOrg's runtime


def test_plugin_loader_registers_the_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    import connectors  # noqa: F401  registers every native connector first
    from connectors import plugins
    from connectors.registry import ConnectorRegistry

    monkeypatch.setattr(ConnectorRegistry, "_connectors", dict(ConnectorRegistry._connectors))
    assert ConnectorRegistry.get("gnani_stt") is None, "a native connector already uses this name"

    outcomes = plugins.load_plugins(enabled=True, allowlist=plugins.parse_allowlist("agenticorg-gnani"))
    mine = [o for o in outcomes if o.name == "gnani_stt"]
    assert [(o.loaded, o.reason, o.distribution) for o in mine] == [(True, "loaded", "agenticorg-gnani")]
    assert ConnectorRegistry.get("gnani_stt") is GnaniSttConnector

    blocked = plugins.load_plugins(enabled=True, allowlist=set())
    assert [o.reason for o in blocked if o.name == "gnani_stt"] == ["not_allowlisted"]


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    import connectors  # noqa: F401
    from connectors.registry import ConnectorRegistry

    monkeypatch.setattr(ConnectorRegistry, "_connectors", dict(ConnectorRegistry._connectors))
    ConnectorRegistry.register(GnaniSttConnector)


def test_agent_tool_has_a_typed_schema(registered: None) -> None:
    from core.langgraph.tool_adapter import build_tools_for_agent

    (tool,) = build_tools_for_agent(["gnani_stt:query"], connector_names=["gnani_stt"])
    assert tool.name == "gnani_stt__query"
    assert len(tool.description) <= 1024 and "audio_url" in tool.description
    props = tool.args_schema.model_json_schema()["properties"]
    assert set(props) == {
        "audio_url",
        "audio_base64",
        "audio_format",
        "language_code",
        "itn",
        "native_numerals",
        "voice_session_id",
        "voice_transcript",
    }
    assert "voice_session_id" in tool.description
    assert props["language_code"]["default"] == "en-IN"
    assert props["itn"]["type"] == "boolean"


async def test_agent_tool_call_end_to_end(registered: None, upstream: Upstream) -> None:
    from core.langgraph import tool_adapter

    upstream.files["https://files.example.com/note.wav"] = httpx.Response(200, content=wav())
    (tool,) = tool_adapter.build_tools_for_agent(
        ["gnani_stt:query"], connector_config={"api_key": KEY}, connector_names=["gnani_stt"]
    )
    result = await tool.ainvoke({"audio_url": "https://files.example.com/note.wav", "language_code": "hi-IN"})
    assert result["transcript"] == "नमस्ते"

    tool_adapter._connector_cache.clear()
    upstream.stt_replies[:] = [httpx.Response(401, json={"detail": "Invalid API key"})]
    failed = await tool.ainvoke({"audio_url": "https://files.example.com/note.wav"})
    assert failed["error"] == "invalid_access_token" and failed["http_status"] == 401
    assert KEY not in str(failed)


async def test_real_runs_pass_agenticorgs_action_policy(registered: None, upstream: Upstream) -> None:
    """A call that carries a tenant goes through the action policy; an unknown tool name is refused."""
    import uuid

    from core.langgraph import tool_adapter

    tool_adapter._connector_cache.clear()
    scope = {"tenant_id": str(uuid.uuid4()), "company_id": str(uuid.uuid4()), "domain": "operations"}
    params = {"audio_base64": b64(wav()), "language_code": "hi-IN"}

    allowed = await tool_adapter._execute_connector_tool("gnani_stt", "query", params, {"gnani_api_key": KEY}, **scope)
    assert allowed["transcript"] == "नमस्ते"

    refused = await tool_adapter._execute_connector_tool(
        "gnani_stt", "transcribe_audio", params, {"gnani_api_key": KEY}, **scope
    )
    assert refused["error"] == "action_contained" and "action_unknown" in refused["message"]
    assert len(upstream.stt_requests) == 1
