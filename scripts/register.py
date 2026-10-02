"""Register the two connectors and the workflow in a running AgenticOrg, and start runs.

Standard library only. Not yet run against a live AgenticOrg server: the
request bodies are checked against AgenticOrg's own request schemas in
tests/test_register.py, and the routes were read from its source (commit
94c62a3). If a call fails, the server's reply is printed in full.

    export AGENTICORG_URL=http://localhost:8000       # default
    export AGENTICORG_TOKEN=...                        # a tenant admin's access token
    export COMPANY_ID=...                              # the company to run under
    export GNANI_API_KEY=...                           # optional; without it gnani_stt is not registered

    python3 scripts/register.py setup
    python3 scripts/register.py run WORKFLOW_ID --voice-session vs_0123456789abcdef
    python3 scripts/register.py run WORKFLOW_ID --audio-url https://.../note.wav --language en-IN \\
        --member DEMO-MEMBER-001 --claim DEMO-CLAIM-001

``setup`` registers ``claim_desk`` and ``gnani_stt`` under the company, runs
each connector's test (an agent or workflow needs them healthy), and creates
the workflow. ``run`` starts a run and follows it until it finishes or waits
for approval; approve in AgenticOrg's Approvals screen.

A run starts from one of two things. ``--voice-session`` is the reference the
live capture page shows after "Stop and save"; the statement is read from the
capture folder on this machine (``~/gnani-voice-sessions``) and sent with the
run, so AgenticOrg can be anywhere. ``--audio-url`` is a public https link to
a recorded clip of 60 seconds or less, which AgenticOrg sends to Gnani.

Secrets are read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parents[1]
DEFINITION = KIT / "workflow" / "health_claim_filing.json"
SESSIONS_DIR = "~/gnani-voice-sessions"  # where the capture page saves; same default as agenticorg_gnani.sessions


# ----------------------------------------------------------- request bodies


def claim_desk_body(company_id: str, outbox_dir: str = "~/claim-desk-outbox", vault_dir: str = "") -> dict[str, Any]:
    # auth_config must not be empty: AgenticOrg stores it encrypted and refuses to test or
    # activate a connector that has nothing stored.
    config = {"claim_desk_outbox_dir": outbox_dir}
    if vault_dir:
        config["claim_desk_vault_dir"] = vault_dir
    return {
        "name": "claim_desk",
        "category": "ops",
        "auth_type": "none",
        "company_id": company_id,
        "auth_config": config,
    }


def gnani_body(company_id: str, api_key: str) -> dict[str, Any]:
    return {
        "name": "gnani_stt",
        "category": "comms",
        "auth_type": "api_key",
        "base_url": "https://api.vachana.ai",
        "company_id": company_id,
        "auth_config": {"gnani_api_key": api_key},
    }


def workflow_body(company_id: str) -> dict[str, Any]:
    definition = json.loads(DEFINITION.read_text(encoding="utf-8"))
    return {
        "name": definition["name"],
        "version": definition["version"],
        "description": definition["description"],
        "domain": definition["domain"],
        "definition": definition,
        "trigger_type": "manual",
        "company_id": company_id,
    }


def read_voice_session(session_id: str, directory: str = "") -> dict[str, Any]:
    """A statement the live capture page saved. Raises ValueError with what to do when it cannot be used."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", session_id):
        raise ValueError(f"{session_id!r} is not a voice session reference; it looks like vs_0123456789abcdef")
    folder = Path(directory or os.environ.get("GNANI_SESSIONS_DIR") or SESSIONS_DIR).expanduser()
    path = folder / f"{session_id}.json"
    if not path.is_file():
        raise ValueError(
            f"No voice session {session_id} in {folder}. Record one on the capture page on this machine, "
            "or pass --sessions-dir if it saves somewhere else."
        )
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path} could not be read: {error}") from error
    if not isinstance(session, dict) or session.get("source") != "gnani_realtime":
        raise ValueError(f"{path} was not written by the capture page")
    if not str(session.get("transcript") or "").strip():
        raise ValueError(f"Voice session {session_id} has no transcript")
    return session


def run_body(
    company_id: str,
    member_id: str,
    claim_ref: str,
    audio_url: str = "",
    language: str = "en-IN",
    voice_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The run's starting data: a live voice session, or a link to a recorded clip."""
    if bool(audio_url) == bool(voice_session):
        raise ValueError("a run starts from a voice session or an audio link: give exactly one")
    payload = {
        "company_id": company_id,  # workflow steps look the company up in the payload
        "member_id": member_id,
        "claim_ref": claim_ref,
        # All three are always sent, empty when unused, so state 1 never sees an unfilled placeholder.
        "voice_session_id": "",
        "voice_transcript": "",
        "audio_url": audio_url,
        "language_code": language,
    }
    if voice_session:
        payload["voice_session_id"] = str(voice_session["session_id"])
        payload["voice_transcript"] = str(voice_session["transcript"]).strip()
        payload["language_code"] = str(voice_session.get("language_code") or language)  # the language spoken
    return {"payload": payload}


# --------------------------------------------------------------------- http


class Api:
    def __init__(self, base_url: str, token: str) -> None:
        self.base = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise SystemExit("AGENTICORG_URL must be https, or http on localhost")
        self.token = token

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        request = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            f"{self.base}/api/v1{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                raw = response.read().decode("utf-8", "replace")
                status = response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read().decode("utf-8", "replace"), error.code
        except urllib.error.URLError as error:
            raise SystemExit(f"Cannot reach {self.base}: {error.reason}") from error
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw


def _need(status: int, reply: Any, what: str) -> Any:
    if status >= 400:
        shown = json.dumps(reply, indent=2) if not isinstance(reply, str) else reply
        raise SystemExit(f"{what} failed: HTTP {status}\n{shown}")
    return reply


def _connector(api: Api, body: dict[str, Any]) -> str:
    """Register a connector, or find it if it is already registered. Returns its id."""
    name, company_id = body["name"], body["company_id"]
    status, reply = api.call("POST", "/connectors", body)
    if status == 409:
        listing = _need(
            *api.call("GET", f"/connectors?company_id={company_id}&per_page=200"), f"listing connectors for {name}"
        )
        match = next((item for item in listing.get("items", []) if item.get("name") == name), None)
        if match is None:
            raise SystemExit(f"{name} already exists in this workspace but is not visible for company {company_id}")
        connector_id = str(match["id"])
        print(f"  {name}: already registered ({connector_id}); its stored config was left as it is")
    else:
        connector_id = str(_need(status, reply, f"registering {name}")["id"])
        print(f"  {name}: registered ({connector_id})")
    tested = _need(*api.call("POST", f"/connectors/{connector_id}/test?company_id={company_id}"), f"testing {name}")
    health = tested.get("health") or {"status": "not tested", "detail": tested.get("error")}
    print(f"  {name}: test -> {health.get('status')}" + ("" if health.get("status") == "healthy" else f"  {health}"))
    return connector_id


def setup(api: Api, company_id: str, gnani_key: str, outbox_dir: str, vault_dir: str) -> int:
    print("Connectors")
    _connector(api, claim_desk_body(company_id, outbox_dir, vault_dir))
    if gnani_key:
        _connector(api, gnani_body(company_id, gnani_key))
    else:
        print("  gnani_stt: skipped, GNANI_API_KEY is not set (state 1 of the workflow will fail without it)")
    print("Workflow")
    created = _need(*api.call("POST", "/workflows", workflow_body(company_id)), "creating the workflow")
    print(f"  health_claim_filing: created ({created['workflow_id']})")
    print(
        "\nStart a run from a statement captured live, or from a recorded clip:\n"
        f"  python3 scripts/register.py run {created['workflow_id']} --voice-session vs_...\n"
        f"  python3 scripts/register.py run {created['workflow_id']} --audio-url https://... --language en-IN"
    )
    return 0


def run(api: Api, workflow_id: str, body: dict[str, Any], wait_seconds: int) -> int:
    started = _need(*api.call("POST", f"/workflows/{workflow_id}/run", body), "starting the run")
    run_id = started["run_id"]
    print(f"Run {run_id} started")
    deadline = time.monotonic() + wait_seconds
    seen: dict[str, str] = {}
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = _need(*api.call("GET", f"/workflows/runs/{run_id}"), "reading the run")
        for step in state.get("steps") or []:
            step_id, status = str(step.get("step_id")), str(step.get("status"))
            if seen.get(step_id) != status:
                seen[step_id] = status
                problem = f"  ({step['error_message']})" if step.get("error_message") else ""
                print(f"  {step_id}: {status}{problem}")
        if state.get("status") != "running":
            break
        time.sleep(3)
    status = state.get("status")
    print(f"Run status: {status}")
    if status == "waiting_hitl":
        print(
            "It is waiting for approval: open Approvals in AgenticOrg, check the filled form in the outbox, and decide."
        )
    return 0 if status in ("completed", "waiting_hitl") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Register and run the claim workflow in AgenticOrg")
    commands = parser.add_subparsers(dest="command", required=True)
    setup_cmd = commands.add_parser("setup", help="register the connectors and the workflow")
    setup_cmd.add_argument("--outbox-dir", default="~/claim-desk-outbox", help="on the AgenticOrg server")
    setup_cmd.add_argument("--vault-dir", default="", help="on the AgenticOrg server; default: the bundled demo vault")
    run_cmd = commands.add_parser("run", help="start a run and follow it")
    run_cmd.add_argument("workflow_id")
    source = run_cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("--voice-session", help="reference shown by the live capture page, like vs_0123456789abcdef")
    source.add_argument("--audio-url", help="public https link to a recorded voice note (60 s or less)")
    run_cmd.add_argument("--sessions-dir", default="", help=f"where the capture page saves; default {SESSIONS_DIR}")
    run_cmd.add_argument("--language", default="en-IN", help="for --audio-url; a voice session knows its language")
    run_cmd.add_argument("--member", default="DEMO-MEMBER-001")
    run_cmd.add_argument("--claim", default="DEMO-CLAIM-001")
    run_cmd.add_argument("--wait", type=int, default=300, help="seconds to follow the run")
    args = parser.parse_args()

    token = os.environ.get("AGENTICORG_TOKEN", "").strip()
    company_id = os.environ.get("COMPANY_ID", "").strip()
    if not token or not company_id:
        print("Set AGENTICORG_TOKEN and COMPANY_ID first.\n")
        print(__doc__)
        return 2
    api = Api(os.environ.get("AGENTICORG_URL", "http://localhost:8000"), token)
    if args.command == "setup":
        return setup(api, company_id, os.environ.get("GNANI_API_KEY", "").strip(), args.outbox_dir, args.vault_dir)
    session = None
    if args.voice_session:
        try:
            session = read_voice_session(args.voice_session, args.sessions_dir)
        except ValueError as error:
            print(error)
            return 2
        words = len(str(session["transcript"]).split())
        print(f"Voice session {session['session_id']}: {words} words, {session.get('language_code')}")
        if session.get("complete") is False:
            print("  note: that recording ended early; the statement may be cut short")
    body = run_body(company_id, args.member, args.claim, args.audio_url or "", args.language, session)
    return run(api, args.workflow_id, body, args.wait)


if __name__ == "__main__":
    sys.exit(main())
