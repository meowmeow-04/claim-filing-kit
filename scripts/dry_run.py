"""Run the whole claim workflow through AgenticOrg's real engine, on this machine, with no server.

    cd <agentic-org checkout>
    python /path/to/claim-filing-kit/scripts/dry_run.py                      # the complete demo claim
    python /path/to/claim-filing-kit/scripts/dry_run.py --claim DEMO-CLAIM-002   # hospital form missing
    python /path/to/claim-filing-kit/scripts/dry_run.py --member DEMO-MEMBER-002 --claim DEMO-CLAIM-003  # lapsed policy
    python /path/to/claim-filing-kit/scripts/dry_run.py --claim DEMO-CLAIM-004   # scanned bill, needs Tesseract
    python /path/to/claim-filing-kit/scripts/dry_run.py --reject             # the insured rejects the form
    python /path/to/claim-filing-kit/scripts/dry_run.py --voice-session vs_0123456789abcdef   # your own words

What is real: AgenticOrg's workflow engine, plugin loader and action policy;
both connectors; reading the vault; OCR; filling the PDF; building the package.
With --voice-session, state 1 is real too: it is the statement you spoke into
the live capture page, as Gnani transcribed it.

What is stood in for, because a dry run has no database, no model key and no
Gnani key: the company, connector-config and feature-flag lookups (an empty
flag store, so every flag is at its default), the four model replies (scripted
below for the demo claim, whatever the statement says) and, without
--voice-session, Gnani's reply (a fixed transcript).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

os.environ.setdefault("AGENTICORG_ENV", "test")
os.environ["AGENTICORG_TEST_FAKE_LLM"] = "1"
sys.path.insert(0, os.getcwd())  # the AgenticOrg checkout

try:
    import httpx
    from connectors import plugins
    from connectors.registry import ConnectorRegistry
    from core.test_doubles import fake_llm
    from workflows import step_types
    from workflows.engine import WorkflowEngine
    from workflows.state_store import InMemoryWorkflowStateRepository, WorkflowStateStore
except ModuleNotFoundError as exc:
    sys.exit(
        f"Cannot import {exc.name!r}. Run this from the AgenticOrg checkout, in the Python environment "
        "that has AgenticOrg's dependencies and both plugins installed (scripts/setup_and_test.sh makes one)."
    )

KIT = Path(__file__).resolve().parents[1]
DEFINITION = KIT / "workflow" / "health_claim_filing.json"
TENANT = "11111111-1111-4111-8111-111111111111"
COMPANY = "22222222-2222-4222-8222-222222222222"
AUDIO_URL = "https://files.example.com/voice-note.wav"

TRANSCRIPT = (
    "On the fourteenth of September I slipped on the wet stairs at home and fell on my right hand. "
    "My wrist swelled up, so I went to Sample City Hospital. They said it was fractured, set it under "
    "anaesthesia and put on a cast. I was discharged the next day."
)


def _line(item: str, amount: int, category: str, expected: str, clause: str) -> dict[str, Any]:
    return {"item": item, "amount": amount, "category": category, "expected": expected, "clause": clause}


def scripted_replies(policy_active: bool) -> dict[str, dict[str, Any]]:
    """What a model is expected to return for the demo documents. Stand-ins, keyed by step action."""
    return {
        "assess_incident": {
            "status": "completed",
            "confidence": 0.93,
            "incident_summary": (
                "On 14 September 2026 the insured slipped on wet stairs at home and fell on her right hand. "
                "She was admitted to Sample City Hospital, where a fracture of the right wrist was set under "
                "anaesthesia and put in a cast. She was discharged the next day."
            ),
            "incident_date": "2026-09-14",
            "cause": "injury",
            "injury_or_illness": "Fracture of the right wrist",
            "pre_existing_related": "no",
            "pre_existing_evidence": [
                {
                    "source": "2025-11-02_annual_health_check.pdf",
                    "quote": "no history of fracture, bone disease, osteoporosis or joint disorder",
                }
            ],
            "open_questions": [],
        },
        "check_eligibility": {
            "status": "completed",
            "confidence": 0.92,
            "decision": "covered" if policy_active else "not_covered",
            "reasons": (
                ["The policy was in force on the admission date.", "Accidental injury is covered from the first day."]
                if policy_active
                else ["The policy period ended before the admission date."]
            ),
            "clauses": [{"clause": "2", "quote": "No waiting period applies to accidental injury."}],
            "open_questions": [],
        },
        "summarise_coverage": {
            "status": "completed",
            "confidence": 0.91,
            "line_items": [
                _line("Registration and admission fee", 300, "Administrative", "not_payable", "4(b)"),
                _line("Emergency consultation", 1200, "Consultation", "payable", "1"),
                _line("Orthopaedic specialist consultation", 1500, "Consultation", "payable", "1"),
                _line("X-ray, right wrist, two views", 900, "Diagnostics", "payable", "1"),
                _line("Pre-procedure blood tests", 1400, "Diagnostics", "payable", "1"),
                _line("Room rent, semi-private, 1 day", 4000, "Room", "payable", "1"),
                _line("Closed reduction and casting, with theatre charges", 18000, "Procedure", "payable", "1"),
                _line("Anaesthesia", 4500, "Procedure", "payable", "1"),
                _line("Take-home medicines dispensed at discharge", 1850, "Pharmacy", "not_payable", "4(a)"),
            ],
            "amount_billed": 33650,
            "amount_claimed": 31500,
            "amount_not_claimed": 2150,
            "not_claimed_items": [
                {"item": "Take-home medicines", "amount": 1850, "reason": "not covered, clause 4(a)"},
                {"item": "Registration and admission fee", "amount": 300, "reason": "not covered, clause 4(b)"},
            ],
            "unreadable": [],
            "checks": {"items_add_up_to_total": True, "matches_hospital_form_total": True},
            "open_questions": [],
        },
        "write_report": {
            "status": "completed",
            "confidence": 0.92,
            "report": (
                "Asha Rao slipped on wet stairs at home on 14 September 2026 and fractured her right wrist. "
                "Sample City Hospital set the fracture under anaesthesia and applied a cast; she was discharged on "
                "15 September 2026. On file: the hospital bill and the hospital's Part B. The policy was in force "
                "and covers accidental injury from the first day. Amount claimed: INR 31,500 of a bill of INR 33,650. "
                "Not claimed: take-home medicines (INR 1,850) and the registration fee (INR 300), both excluded."
            ),
            "summary_for_approver": [
                "Claiming INR 31,500 of INR 33,650.",
                "Not claiming INR 2,150: take-home medicines and the registration fee are excluded.",
                "You must sign Part A before sending it.",
            ],
            "open_questions": [],
        },
    }


def _gnani_stand_in(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.vachana.ai":
        return httpx.Response(200, json={"success": True, "request_id": "req_dryrun", "transcript": TRANSCRIPT})
    return httpx.Response(200, content=b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64)  # the voice note download


def _observation(step_id: str, result: dict[str, Any]) -> str:
    status = result.get("status")
    out = result.get("output") if isinstance(result.get("output"), dict) else {}
    if status == "skipped":
        return "skipped (branch not taken)"
    if status in ("failed", "rejected"):
        return f"{status}: {result.get('error')}"
    if step_id == "transcribe":
        text = str(out.get("transcript", ""))
        how = "captured live, Gnani realtime" if out.get("source") == "gnani_realtime" else "recorded clip, Gnani"
        return f'transcript ({how}, {out.get("language_code")}): "{text[:70]}{"..." if len(text) > 70 else ""}"'
    if "documents" in out:
        docs = [
            f"{d['file']} ({d.get('method') or 'unreadable'}{', OCR' if d.get('ocr') else ''})"
            for d in out["documents"]
        ]
        extra = f", complete={out['complete']}" if "complete" in out else ""
        return f"found={out.get('found')}{extra}: {', '.join(docs) or 'no file'}"
    if step_id == "checks":
        return (
            f"policy {out.get('policy_status')} on {out.get('checked_for_date')}; "
            f"bill {out.get('bill')}; hospital form {out.get('hospital_form')}"
        )
    if step_id == "incident":
        return f"{out.get('injury_or_illness')}; pre-existing related: {out.get('pre_existing_related')}"
    if step_id == "eligibility":
        return f"decision: {out.get('decision')}"
    if step_id == "coverage":
        return (
            f"billed {out.get('amount_billed')}, claim {out.get('amount_claimed')}, "
            f"not claimed {out.get('amount_not_claimed')}"
        )
    if step_id == "report":
        return f"report drafted ({len(str(out.get('report', '')).split())} words)"
    if "pdf_path" in out:
        return f"{out.get('status')}: {out['pdf_path']} (blank fields: {len(out.get('blank_fields', []))})"
    if step_id == "package":
        receipt = out.get("submission", {}).get("confirmation_id")
        return f"{out.get('status')}: {out.get('package_dir')}; receipt {receipt} (simulated)"
    if "decision" in out:
        return f"decision: {out['decision']}"
    if "branch_target" in result:
        return f"continue with: {result['branch_target']}"
    return str(status)


async def run(
    member_id: str,
    claim_ref: str,
    *,
    decision: str = "approve",
    outbox: Path | None = None,
    replies: dict[str, Any] | None = None,
    gnani: Any = None,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the workflow once. Returns the final status, the step results and a printable trace.

    ``replies`` overrides scripted model replies by action (a string is sent
    as-is, so a test can make the model answer with something that is not
    JSON); ``gnani`` replaces the stand-in for Gnani's API; ``payload_extra``
    changes the run's starting data (a value of None leaves that key out).
    """
    definition = json.loads(DEFINITION.read_text(encoding="utf-8"))
    outbox = outbox or Path(tempfile.mkdtemp(prefix="claim-desk-outbox-"))
    configs = {"gnani_stt": {"gnani_api_key": "dry-run-key"}, "claim_desk": {"claim_desk_outbox_dir": str(outbox)}}

    for name in ("gnani_stt", "claim_desk"):
        ConnectorRegistry._connectors.pop(name, None)
    loaded = plugins.load_plugins(
        enabled=True, allowlist=plugins.parse_allowlist("agenticorg-gnani,agenticorg-claimdesk")
    )
    missing = {"gnani_stt", "claim_desk"} - {o.name for o in loaded if o.loaded}
    if missing:
        raise RuntimeError(f"plugin(s) not installed or not loaded: {sorted(missing)}")
    gnani_cls = ConnectorRegistry.get("gnani_stt")
    claim_desk = ConnectorRegistry.get("claim_desk")({"claim_desk_outbox_dir": str(outbox)})
    try:  # only to choose which scripted model replies fit this claim
        status = await claim_desk.check_status(member_id, claim_ref)
    except (ValueError, LookupError):
        status = {"policy_status": "active"}

    from core.langgraph import tool_adapter

    tool_adapter._connector_cache.clear()
    fake_llm.reset()
    scripted = {**scripted_replies(status["policy_status"] == "active"), **(replies or {})}
    for action, reply in scripted.items():
        content = reply if isinstance(reply, str) else json.dumps(reply)
        fake_llm.register_response(prompt_contains=f'"action": "{action}"', content=content)

    async def company(_tenant: str, _company: Any) -> uuid.UUID:
        return uuid.UUID(COMPANY)

    async def connector_config(connector: str, _tenant: str, _company: Any) -> dict[str, Any]:
        return dict(configs.get(connector, {}))

    async def no_flags(_key: str, **_scope: Any) -> SimpleNamespace:
        return SimpleNamespace(global_row=None, tenant_row=None)

    engine = WorkflowEngine(WorkflowStateStore(repository=InMemoryWorkflowStateRepository(), redis=None))
    payload = {
        "company_id": COMPANY,
        "member_id": member_id,
        "claim_ref": claim_ref,
        "voice_session_id": "",
        "voice_transcript": "",
        "audio_url": AUDIO_URL,
        "language_code": "en-IN",
        **(payload_extra or {}),
    }
    payload = {key: value for key, value in payload.items() if value is not None}  # None: leave it out of the run
    paused = ""
    with (
        patch.object(step_types, "_validated_workflow_company", company),
        patch.object(step_types, "_load_workflow_connector_config", connector_config),
        patch("core.feature_flags.load_flag_rows_strict", no_flags),
        patch.object(
            gnani_cls,
            "_connector_transport_and_dns",
            lambda self: (httpx.MockTransport(gnani or _gnani_stand_in), False),
        ),
    ):
        run_id = await engine.start_run(definition, payload, tenant_id=TENANT)
        result = await engine.execute(run_id)
        if result.get("status") == "waiting_hitl":
            paused = f"    (the run paused here for the insured; the dry run answered: {decision})\n"
            result = await engine.resume_from_hitl(run_id, {"decision": decision, "notes": "dry run"})
    state = await engine.state_store.load(run_id)
    steps = state["step_results"]
    trace = []
    for step in definition["steps"]:
        if step["id"] in steps:
            note = paused if step["type"] == "human_in_loop" else ""
            trace.append(f"{step['name']}\n{note}    -> {_observation(step['id'], steps[step['id']])}")
    return {
        "status": result.get("status"),
        "steps": steps,
        "trace": trace,
        "outbox": outbox,
        "calls": fake_llm.call_count(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--member", default="DEMO-MEMBER-001")
    parser.add_argument("--claim", default="DEMO-CLAIM-001")
    parser.add_argument("--reject", action="store_true", help="answer the approval with reject")
    parser.add_argument("--outbox", type=Path, default=Path.home() / "claim-desk-outbox")
    parser.add_argument("--voice-session", help="start from a statement captured live, like vs_0123456789abcdef")
    parser.add_argument(
        "--sessions-dir", default="", help="where the capture page saves; default ~/gnani-voice-sessions"
    )
    args = parser.parse_args()

    extra: dict[str, Any] = {}
    if args.voice_session:
        from agenticorg_gnani.sessions import load_session, sessions_dir

        folder = sessions_dir(args.sessions_dir or os.environ.get("GNANI_SESSIONS_DIR"))
        try:
            session = load_session(folder, args.voice_session)
        except (LookupError, ValueError) as exc:
            print(f"Cannot use that voice session: {exc}")
            return 2
        extra = {  # the same four values scripts/register.py sends with a real run
            "voice_session_id": session["session_id"],
            "voice_transcript": str(session.get("transcript") or ""),
            "audio_url": "",
            "language_code": session.get("language_code") or "en-IN",
        }

    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    outcome = asyncio.run(
        run(
            args.member,
            args.claim,
            decision="reject" if args.reject else "approve",
            outbox=args.outbox,
            payload_extra=extra,
        )
    )
    print(f"\nDry run: {args.member} / {args.claim}\n")
    print("\n".join(outcome["trace"]))
    if extra:
        print(
            "\nState 1 used your statement. States 2 to 5 are scripted for the demo claim in a dry run, so they\n"
            "do not react to what you said; on your AgenticOrg, with a model configured, they do."
        )
    print(f"\nRun status: {outcome['status']}. Model calls (scripted): {outcome['calls']}. Outbox: {outcome['outbox']}")
    return 0 if outcome["status"] in ("completed", "failed") else 1


if __name__ == "__main__":
    sys.exit(main())
