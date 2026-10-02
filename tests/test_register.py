"""scripts/register.py against a stand-in server that validates every body with AgenticOrg's own request schemas."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from core.schemas.api import ConnectorCreate, WorkflowCreate, WorkflowRunTrigger
from workflows.parser import WorkflowParser

KIT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("register", KIT / "scripts" / "register.py")
register = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register)

COMPANY = "22222222-2222-4222-8222-222222222222"


def test_bodies_validate_against_agenticorgs_schemas() -> None:
    desk = ConnectorCreate(**register.claim_desk_body(COMPANY))
    assert desk.auth_config, "an empty auth_config cannot be tested or activated in AgenticOrg"
    gnani = ConnectorCreate(**register.gnani_body(COMPANY, "k"))
    assert gnani.auth_config == {"gnani_api_key": "k"} and gnani.base_url == "https://api.vachana.ai"
    workflow = WorkflowCreate(**register.workflow_body(COMPANY))
    WorkflowParser().parse(workflow.definition)
    assert workflow.company_id == COMPANY and workflow.domain == "operations"
    trigger = WorkflowRunTrigger(
        **register.run_body(COMPANY, "DEMO-MEMBER-001", "DEMO-CLAIM-001", "https://x/y.wav", "hi-IN")
    )
    assert trigger.payload["company_id"] == COMPANY and trigger.payload["language_code"] == "hi-IN"
    assert trigger.payload["audio_url"] == "https://x/y.wav"
    assert trigger.payload["voice_session_id"] == "" and trigger.payload["voice_transcript"] == ""


STATEMENT = "I slipped on the wet stairs at home and broke my right wrist"


def saved(folder: Path, session_id: str = "vs_0123456789abcdef", **changes: object) -> str:
    session = {
        "session_id": session_id,
        "source": "gnani_realtime",
        "language_code": "hi-IN",
        "complete": True,
        "transcript": f" {STATEMENT} ",
        **changes,
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")
    return session_id


def test_a_run_from_a_voice_session_carries_the_statement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = register.read_voice_session(saved(tmp_path), str(tmp_path))
    body = register.run_body(COMPANY, "DEMO-MEMBER-001", "DEMO-CLAIM-001", language="en-IN", voice_session=session)
    assert WorkflowRunTrigger(**body).payload == {
        "company_id": COMPANY,
        "member_id": "DEMO-MEMBER-001",
        "claim_ref": "DEMO-CLAIM-001",
        "voice_session_id": "vs_0123456789abcdef",
        "voice_transcript": STATEMENT,
        "audio_url": "",
        "language_code": "hi-IN",  # the language spoken, not the --language default
    }
    monkeypatch.setenv("GNANI_SESSIONS_DIR", str(tmp_path))
    assert register.read_voice_session("vs_0123456789abcdef")["transcript"].strip() == STATEMENT


def test_a_run_starts_from_exactly_one_source(tmp_path: Path) -> None:
    session = register.read_voice_session(saved(tmp_path), str(tmp_path))
    with pytest.raises(ValueError, match="exactly one"):
        register.run_body(COMPANY, "M", "C")
    with pytest.raises(ValueError, match="exactly one"):
        register.run_body(COMPANY, "M", "C", "https://x/y.wav", voice_session=session)


@pytest.mark.parametrize(
    ("session_id", "message"),
    [
        ("vs_ffffffffffffffff", "No voice session vs_ffffffffffffffff"),
        ("../../etc/passwd", "is not a voice session reference"),
        ("vs_empty", "has no transcript"),
        ("vs_foreign", "was not written by the capture page"),
        ("vs_broken", "could not be read"),
    ],
)
def test_voice_sessions_that_cannot_be_used_say_why(tmp_path: Path, session_id: str, message: str) -> None:
    saved(tmp_path, "vs_empty", transcript="  ")
    saved(tmp_path, "vs_foreign", source="someone_else")
    (tmp_path / "vs_broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        register.read_voice_session(session_id, str(tmp_path))


class _Server:
    """The routes register.py uses, answering in the shapes AgenticOrg's handlers return."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.existing = existing or set()
        self.polls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status: int, body: object) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> dict:
                length = int(self.headers.get("content-length") or 0)
                return json.loads(self.rfile.read(length)) if length else {}

            def do_GET(self) -> None:  # noqa: N802
                outer.calls.append(("GET", self.path))
                assert self.headers["authorization"] == "Bearer token-123"
                if self.path.startswith("/api/v1/connectors?"):
                    return self._reply(200, {"items": [{"id": f"id-{n}", "name": n} for n in sorted(outer.existing)]})
                if self.path.startswith("/api/v1/workflows/runs/"):
                    outer.polls += 1
                    done = outer.polls >= 2
                    steps = [{"step_id": "transcribe", "status": "completed", "error_message": None}]
                    if done:
                        steps.append({"step_id": "approval", "status": "waiting_hitl", "error_message": None})
                    return self._reply(200, {"status": "waiting_hitl" if done else "running", "steps": steps})
                return self._reply(404, {"detail": "Not Found"})

            def do_POST(self) -> None:  # noqa: N802
                outer.calls.append(("POST", self.path))
                assert self.headers["authorization"] == "Bearer token-123"
                body = self._body()
                outer.bodies.append(body)
                if self.path == "/api/v1/connectors":
                    created = ConnectorCreate(**body)
                    if created.name in outer.existing:
                        return self._reply(409, {"detail": f"Connector '{created.name}' already exists"})
                    outer.existing.add(created.name)
                    return self._reply(201, {"id": f"id-{created.name}", "name": created.name})
                if self.path.endswith(f"/test?company_id={COMPANY}"):
                    return self._reply(200, {"tested": True, "health": {"status": "healthy"}})
                if self.path == "/api/v1/workflows":
                    WorkflowCreate(**body)
                    return self._reply(201, {"workflow_id": "wf-1"})
                if self.path == "/api/v1/workflows/wf-1/run":
                    WorkflowRunTrigger(**body)
                    return self._reply(200, {"run_id": "run-1", "status": "running"})
                return self._reply(404, {"detail": "Not Found"})

            def log_message(self, *args: object) -> None:
                pass

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(register.time, "sleep", lambda _s: None)


def test_setup_registers_tests_and_creates_the_workflow(no_proxy: None, capsys: pytest.CaptureFixture) -> None:
    server = _Server()
    code = register.setup(register.Api(server.url, "token-123"), COMPANY, "gnani-secret-key", "~/claim-desk-outbox", "")
    out = capsys.readouterr().out
    assert code == 0 and "gnani-secret-key" not in out and "wf-1" in out
    assert server.calls == [
        ("POST", "/api/v1/connectors"),
        ("POST", f"/api/v1/connectors/id-claim_desk/test?company_id={COMPANY}"),
        ("POST", "/api/v1/connectors"),
        ("POST", f"/api/v1/connectors/id-gnani_stt/test?company_id={COMPANY}"),
        ("POST", "/api/v1/workflows"),
    ]


def test_setup_reuses_a_connector_that_already_exists_and_skips_gnani_without_a_key(
    no_proxy: None, capsys: pytest.CaptureFixture
) -> None:
    server = _Server(existing={"claim_desk"})
    assert register.setup(register.Api(server.url, "token-123"), COMPANY, "", "~/claim-desk-outbox", "") == 0
    out = capsys.readouterr().out
    assert "already registered (id-claim_desk)" in out and "gnani_stt: skipped" in out
    assert ("POST", f"/api/v1/connectors/id-claim_desk/test?company_id={COMPANY}") in server.calls


def test_run_follows_the_run_until_it_waits_for_approval(no_proxy: None, capsys: pytest.CaptureFixture) -> None:
    server = _Server()
    body = register.run_body(COMPANY, "DEMO-MEMBER-001", "DEMO-CLAIM-001", "https://files.example.com/a.wav", "en-IN")
    assert register.run(register.Api(server.url, "token-123"), "wf-1", body, wait_seconds=30) == 0
    out = capsys.readouterr().out
    assert "transcribe: completed" in out and "approval: waiting_hitl" in out and "waiting for approval" in out


def test_run_command_sends_the_voice_session(
    no_proxy: None, capsys: pytest.CaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _Server()
    monkeypatch.setenv("AGENTICORG_URL", server.url)
    monkeypatch.setenv("AGENTICORG_TOKEN", "token-123")
    monkeypatch.setenv("COMPANY_ID", COMPANY)
    session_id = saved(tmp_path, complete=False)
    monkeypatch.setattr(
        "sys.argv", ["register.py", "run", "wf-1", "--voice-session", session_id, "--sessions-dir", str(tmp_path)]
    )
    assert register.main() == 0
    out = capsys.readouterr().out
    assert f"Voice session {session_id}: 13 words, hi-IN" in out and "ended early" in out
    payload = server.bodies[-1]["payload"]
    assert payload["voice_transcript"] == STATEMENT and payload["voice_session_id"] == session_id
    assert payload["audio_url"] == "" and payload["language_code"] == "hi-IN"

    calls = len(server.calls)
    monkeypatch.setattr(
        "sys.argv",
        ["register.py", "run", "wf-1", "--voice-session", "vs_ffffffffffffffff", "--sessions-dir", str(tmp_path)],
    )
    assert register.main() == 2
    assert "No voice session" in capsys.readouterr().out and len(server.calls) == calls, "nothing is started"

    monkeypatch.setattr("sys.argv", ["register.py", "run", "wf-1"])
    with pytest.raises(SystemExit):  # neither --voice-session nor --audio-url
        register.main()


def test_only_https_or_local_http_is_accepted() -> None:
    register.Api("https://agenticorg.example.com", "t")
    register.Api("http://localhost:8000", "t")
    with pytest.raises(SystemExit):
        register.Api("http://agenticorg.example.com", "t")
