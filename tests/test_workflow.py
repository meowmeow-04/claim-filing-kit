"""The claim workflow, run through AgenticOrg's real engine (see scripts/dry_run.py for what is stood in).

Run from the AgenticOrg checkout, in an environment with both plugins installed.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import httpx
import pytest

KIT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dry_run", KIT / "scripts" / "dry_run.py")
dry_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dry_run)

from agenticorg_claimdesk.forms import read_fields  # noqa: E402

DEFINITION = json.loads((KIT / "workflow" / "health_claim_filing.json").read_text(encoding="utf-8"))
STEP_IDS = [step["id"] for step in DEFINITION["steps"]]
AFTER_ELIGIBILITY = STEP_IDS[STEP_IDS.index("bill") :]


def statuses(outcome: dict) -> dict[str, str]:
    return {step_id: result["status"] for step_id, result in outcome["steps"].items()}


def output(outcome: dict, step_id: str) -> dict:
    return outcome["steps"][step_id]["output"]


# ----------------------------------------------------------------- definition


def test_definition_is_current_and_valid() -> None:
    from workflows.parser import WorkflowParser

    spec = importlib.util.spec_from_file_location("build_workflow", KIT / "workflow" / "build_workflow.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert builder.DEFINITION == DEFINITION, "run workflow/build_workflow.py: the JSON is out of date"
    WorkflowParser().parse(DEFINITION)
    assert DEFINITION["domain"] == "operations"


async def test_every_connector_step_names_a_tool_the_action_policy_allows() -> None:
    from core.governance.action_policy import ActionContext, evaluate_action

    for step in DEFINITION["steps"]:
        if step["type"] != "connector_tool":
            continue
        action = f"{step['connector']}:{step['tool']}"
        decision = await evaluate_action(
            action, context=ActionContext(dry_run.TENANT, dry_run.COMPANY, DEFINITION["domain"], "production")
        )
        assert decision.dispatch_allowed, (action, decision.reason)


def test_the_analyst_has_no_tools_and_the_package_step_follows_the_approval() -> None:
    by_id = {step["id"]: step for step in DEFINITION["steps"]}
    for step in DEFINITION["steps"]:
        if step["type"] == "agent":
            assert step["authorized_tools"] == [] and "Reply with one JSON object" in step["system_prompt_text"]
    assert by_id["package"]["depends_on"] == ["approval"]
    assert by_id["package"]["params"]["approval"] == "$approval"
    assert by_id["approval"]["type"] == "human_in_loop"


# ----------------------------------------------------------------------- runs


async def test_complete_claim_runs_all_eight_states(tmp_path: Path) -> None:
    outcome = await dry_run.run("DEMO-MEMBER-001", "DEMO-CLAIM-001", outbox=tmp_path)
    assert outcome["status"] == "completed" and outcome["calls"] == 4
    result = statuses(outcome)
    assert result.pop("stop_not_eligible") == "skipped" and result.pop("hospital_form_needed") == "skipped"
    assert set(result.values()) == {"completed"} and len(result) == len(STEP_IDS) - 2

    assert output(outcome, "transcribe")["transcript"].startswith("On the fourteenth")
    assert output(outcome, "checks")["policy_status"] == "active"
    assert output(outcome, "approval")["decision"] == "approve"

    form = read_fields(Path(output(outcome, "fill_form")["pdf_path"]).read_bytes())
    assert form["amount_claimed"] == "31500" and form["amount_not_claimed"] == "2150"
    assert form["incident_description"].startswith("On 14 September 2026")
    assert form["insured_signature"] == ""

    package = output(outcome, "package")
    assert package["status"] == "package_ready" and package["submission"]["mode"] == "simulated"
    assert len(package["files"]) == 3 and Path(package["package_dir"], "manifest.json").is_file()


async def test_rejection_stops_the_run_before_anything_is_packaged(tmp_path: Path) -> None:
    outcome = await dry_run.run("DEMO-MEMBER-001", "DEMO-CLAIM-001", decision="reject", outbox=tmp_path)
    assert outcome["status"] == "failed"
    assert outcome["steps"]["approval"]["status"] == "rejected" and "package" not in outcome["steps"]
    assert not (tmp_path / "DEMO-MEMBER-001" / "DEMO-CLAIM-001" / "package").exists()
    assert (tmp_path / "DEMO-MEMBER-001" / "DEMO-CLAIM-001" / "claim_form_part_a.pdf").is_file()


async def test_defer_is_not_an_approval(tmp_path: Path) -> None:
    outcome = await dry_run.run("DEMO-MEMBER-001", "DEMO-CLAIM-001", decision="defer", outbox=tmp_path)
    assert outcome["status"] == "failed" and outcome["steps"]["package"]["status"] == "failed"
    assert "not been approved" in json.dumps(outcome["steps"]["package"])
    assert not (tmp_path / "DEMO-MEMBER-001" / "DEMO-CLAIM-001" / "package").exists()


async def test_missing_hospital_form_prepares_part_b_and_stops(tmp_path: Path) -> None:
    outcome = await dry_run.run("DEMO-MEMBER-001", "DEMO-CLAIM-002", outbox=tmp_path)
    assert outcome["status"] == "completed"
    result = statuses(outcome)
    assert result["approval"] == "skipped" and result["package"] == "skipped"
    assert result["fill_form"] == "completed" and result["hospital_form_needed"] == "completed"
    # with no hospital form, the policy is checked on the date the insured gave
    assert output(outcome, "checks")["date_source"] == "incident_result"
    assert output(outcome, "checks")["checked_for_date"] == "2026-09-14"
    request = output(outcome, "hospital_form_needed")
    assert request["status"] == "hospital_form_requested"
    assert read_fields(Path(request["pdf_path"]).read_bytes())["patient_name"] == "Asha Rao"
    assert "hospital_name" in output(outcome, "fill_form")["blank_fields"]


async def test_lapsed_policy_stops_after_eligibility(tmp_path: Path) -> None:
    outcome = await dry_run.run("DEMO-MEMBER-002", "DEMO-CLAIM-003", outbox=tmp_path)
    assert outcome["status"] == "completed" and outcome["calls"] == 2
    result = statuses(outcome)
    assert result["stop_not_eligible"] == "completed"
    assert all(result[step_id] == "skipped" for step_id in AFTER_ELIGIBILITY)
    assert output(outcome, "stop_not_eligible")["checks"]["output"]["policy_status"] == "not_active"
    assert not any(tmp_path.rglob("*.pdf"))


async def test_model_saying_covered_cannot_override_a_lapsed_policy(tmp_path: Path) -> None:
    covered = {"status": "completed", "confidence": 0.99, "decision": "covered"}
    outcome = await dry_run.run(
        "DEMO-MEMBER-002", "DEMO-CLAIM-003", outbox=tmp_path, replies={"check_eligibility": covered}
    )
    assert statuses(outcome)["stop_not_eligible"] == "completed" and statuses(outcome)["fill_form"] == "skipped"


@pytest.mark.parametrize(
    "reply", ["I think this is probably covered.", {"status": "completed"}, {"decision": "not_covered"}]
)
async def test_unusable_eligibility_answer_stops_the_run(tmp_path: Path, reply) -> None:
    outcome = await dry_run.run(
        "DEMO-MEMBER-001", "DEMO-CLAIM-001", outbox=tmp_path, replies={"check_eligibility": reply}
    )
    assert outcome["status"] == "completed"
    assert statuses(outcome)["stop_not_eligible"] == "completed" and statuses(outcome)["package"] == "skipped"


async def test_needs_review_continues_to_the_human_approval(tmp_path: Path) -> None:
    review = {"status": "completed", "confidence": 0.9, "decision": "needs_review"}
    outcome = await dry_run.run(
        "DEMO-MEMBER-001", "DEMO-CLAIM-001", outbox=tmp_path, replies={"check_eligibility": review}
    )
    assert statuses(outcome)["approval"] == "completed" and statuses(outcome)["package"] == "completed"


STATEMENT = "On the fourteenth of September I slipped on the wet stairs at home and broke my right wrist."
SESSION_ID = "vs_0123456789abcdef"


class GnaniCalls:
    """Counts calls to Gnani's recorded-clip API; a statement captured live must cause none."""

    def __init__(self) -> None:
        self.hosts: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        return httpx.Response(500)


async def test_a_statement_captured_live_becomes_state_one(tmp_path: Path) -> None:
    """What scripts/register.py sends for --voice-session: the reference and the text, no audio link."""
    gnani = GnaniCalls()
    live = {"voice_session_id": SESSION_ID, "voice_transcript": STATEMENT, "audio_url": "", "language_code": "hi-IN"}
    outcome = await dry_run.run("DEMO-MEMBER-001", "DEMO-CLAIM-001", outbox=tmp_path, payload_extra=live, gnani=gnani)
    assert outcome["status"] == "completed" and statuses(outcome)["package"] == "completed"
    assert output(outcome, "transcribe") == {
        "transcript": STATEMENT,
        "language_code": "hi-IN",
        "format": "verbatim",
        "source": "gnani_realtime",
        "voice_session_id": SESSION_ID,
        "read_from": "run",
    }
    assert gnani.hosts == [], "it was transcribed while it was spoken"


async def test_a_run_given_only_the_reference_reads_the_capture_folder(tmp_path: Path, monkeypatch) -> None:
    """A run started by hand with just voice_session_id: every other state-1 placeholder stays unfilled."""
    from agenticorg_gnani.sessions import save_session

    folder = tmp_path / "sessions"
    save_session(
        folder,
        {
            "session_id": SESSION_ID,
            "source": "gnani_realtime",
            "language_code": "ta-IN",
            "transcript": STATEMENT,
            "complete": True,
        },
    )
    monkeypatch.setenv("GNANI_SESSIONS_DIR", str(folder))
    gnani = GnaniCalls()
    only_reference = {
        "voice_session_id": SESSION_ID,
        "voice_transcript": None,
        "audio_url": None,
        "language_code": None,
    }
    outcome = await dry_run.run(
        "DEMO-MEMBER-001", "DEMO-CLAIM-001", outbox=tmp_path / "outbox", payload_extra=only_reference, gnani=gnani
    )
    assert outcome["status"] == "completed"
    said = output(outcome, "transcribe")
    assert (
        said["transcript"] == STATEMENT and said["language_code"] == "ta-IN" and said["read_from"] == "capture_folder"
    )
    assert gnani.hosts == []


@pytest.mark.parametrize(
    ("start", "problem"),
    [
        ({"voice_session_id": None, "voice_transcript": None, "audio_url": None}, "exactly one of"),
        ({"voice_session_id": "", "voice_transcript": "", "audio_url": ""}, "exactly one of"),
        ({"voice_session_id": "vs_ffffffffffffffff", "audio_url": ""}, "No voice session"),
        ({"voice_session_id": SESSION_ID, "voice_transcript": STATEMENT}, "not both"),
    ],
)
async def test_a_run_without_a_usable_statement_fails_at_state_one(tmp_path: Path, monkeypatch, start, problem) -> None:
    monkeypatch.setenv("GNANI_SESSIONS_DIR", str(tmp_path / "empty"))
    gnani = GnaniCalls()
    outcome = await dry_run.run("DEMO-MEMBER-001", "DEMO-CLAIM-001", outbox=tmp_path, payload_extra=start, gnani=gnani)
    assert outcome["status"] == "failed" and list(outcome["steps"]) == ["transcribe"]
    assert problem in json.dumps(outcome["steps"]["transcribe"])
    assert gnani.hosts == []


async def test_gnani_failure_fails_the_run_at_state_one(tmp_path: Path) -> None:
    def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.vachana.ai":
            return httpx.Response(401, json={"detail": "Invalid API key"})
        return httpx.Response(200, content=b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64)

    outcome = await dry_run.run("DEMO-MEMBER-001", "DEMO-CLAIM-001", outbox=tmp_path, gnani=rejected)
    assert outcome["status"] == "failed" and list(outcome["steps"]) == ["transcribe"]
    assert "dry-run-key" not in json.dumps(outcome["steps"])


async def test_bad_member_id_fails_closed(tmp_path: Path) -> None:
    outcome = await dry_run.run("../../etc", "DEMO-CLAIM-001", outbox=tmp_path)
    assert outcome["status"] == "failed" and list(outcome["steps"]) == ["transcribe", "medical_records"]
    assert "letters, digits" in json.dumps(outcome["steps"]["medical_records"])


@pytest.mark.skipif(
    not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="Tesseract and Poppler are needed for OCR"
)
async def test_scanned_bill_claim(tmp_path: Path) -> None:
    pytest.importorskip("pytesseract")
    pytest.importorskip("pdf2image")
    outcome = await dry_run.run("DEMO-MEMBER-001", "DEMO-CLAIM-004", outbox=tmp_path)
    assert outcome["status"] == "completed"
    (document,) = output(outcome, "bill")["documents"]
    assert document["ocr"]["pages"] == [1] and "33,650" in document["text"]
    assert output(outcome, "package")["files"][2]["file"] == "hospital_bill_scan.pdf"
