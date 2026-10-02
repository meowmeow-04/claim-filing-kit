"""Tests for the claim-desk connector. Run inside AgenticOrg's environment, from its checkout.

No network. Reads the bundled synthetic vault; writes only to a temp outbox.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from agenticorg_claimdesk.connector import DEMO_DIR, ClaimDeskConnector, _amount, _items_text, _payload
from agenticorg_claimdesk.forms import fill_form, read_fields
from agenticorg_claimdesk.formspec import FORMS, HUMAN_ONLY_FIELDS, field_names

MEMBER, CLAIM = "DEMO-MEMBER-001", "DEMO-CLAIM-001"
APPROVED = {"output": {"decision": "approve", "notes": None}, "status": "completed", "confidence": None}
OCR_READY = bool(shutil.which("tesseract") and shutil.which("pdftoppm"))
try:
    import pdf2image  # noqa: F401
    import pytesseract  # noqa: F401
except ImportError:
    OCR_READY = False

INCIDENT = {
    "output": {
        "status": "completed",
        "incident_summary": "She slipped on wet stairs and broke her right wrist.",
        "incident_date": "2026-09-14",
        "cause": "injury",
        "injury_or_illness": "Fracture of the right wrist",
    },
    "status": "completed",
    "confidence": 0.93,
}
COVERAGE = {
    "output": {
        "amount_billed": 33650,
        "amount_claimed": "31,500",
        "amount_not_claimed": 2150,
        "not_claimed_items": [{"item": "Take-home medicines", "amount": 1850, "reason": "clause 4(a)"}],
    },
    "status": "completed",
}


@pytest.fixture
async def desk(tmp_path: Path) -> ClaimDeskConnector:
    connector = ClaimDeskConnector({"claim_desk_outbox_dir": str(tmp_path / "outbox")})
    await connector.connect()
    return connector


async def draft(desk: ClaimDeskConnector, claim: str = CLAIM, **extra) -> dict:
    return await desk.execute_tool("prepare_draft", {"member_id": MEMBER, "claim_ref": claim, **extra})


# --------------------------------------------------------------------- helpers


def test_payload_unwraps_step_results_and_json_text() -> None:
    assert _payload({"output": {"a": 1}, "status": "completed"}) == {"a": 1}
    assert _payload('{"a": 1}') == {"a": 1}
    assert _payload("$incident") == {"text": "$incident"}
    assert _payload(None) == {}
    assert _payload({"raw_output": "not json", "status": "completed"})["raw_output"] == "not json"


@pytest.mark.parametrize(
    ("raw", "number"),
    [("31,500", 31500.0), ("INR 1,850.50", 1850.5), (300, 300.0), ("", None), (None, None), (True, None)],
)
def test_amount_parsing(raw, number) -> None:
    assert _amount(raw) == number


def test_items_text() -> None:
    items = [{"item": "Medicines", "amount": "1,850", "reason": "excluded"}, {"item": "Fee"}, "note"]
    assert _items_text(items) == "Medicines (INR 1850): excluded; Fee; note"


# ------------------------------------------------------------------------ pdf


def test_templates_are_fillable_and_match_the_spec() -> None:
    for form, spec in FORMS.items():
        fields = read_fields((DEMO_DIR / "forms" / spec["file"]).read_bytes())
        assert set(fields) == set(field_names(form)), form
        assert all(value == "" for value in fields.values())


def test_fill_form_round_trip_and_field_map() -> None:
    template = (DEMO_DIR / "forms" / "claim_form_part_a.pdf").read_bytes()
    pdf, missing = fill_form(template, {"policy_number": "P-1", "amount_claimed": 31500, "no_such_field": "x"})
    assert missing == ["no_such_field"]
    fields = read_fields(pdf)
    assert fields["policy_number"] == "P-1" and fields["amount_claimed"] == "31500"

    pdf, missing = fill_form(template, {"their_name": "P-2"}, field_map={"their_name": "policy_number"})
    assert missing == [] and read_fields(pdf)["policy_number"] == "P-2"


# ---------------------------------------------------------------------- vault


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".", "$member_id", "x" * 65, "a b"])
async def test_member_and_claim_ids_cannot_escape_the_vault(desk: ClaimDeskConnector, bad: str) -> None:
    with pytest.raises(ValueError, match="letters, digits"):
        await desk.execute_tool("list_records", {"member_id": bad})
    with pytest.raises(ValueError, match="letters, digits"):
        await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "bill", "claim_ref": bad or "/"})


async def test_symlink_out_of_the_vault_is_ignored(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    shutil.copytree(DEMO_DIR / "vault", vault)
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the model", encoding="utf-8")
    (vault / "members" / MEMBER / "medical" / "linked.txt").symlink_to(secret)
    desk = ClaimDeskConnector({"claim_desk_vault_dir": str(vault), "claim_desk_outbox_dir": str(tmp_path / "out")})
    result = await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "medical_records"})
    assert "linked.txt" not in [d["file"] for d in result["documents"]]
    assert "not for the model" not in json.dumps(result)


async def test_unknown_member_and_unknown_record(desk: ClaimDeskConnector) -> None:
    with pytest.raises(LookupError):
        await desk.execute_tool("list_records", {"member_id": "NO-SUCH-MEMBER"})
    with pytest.raises(ValueError, match="record must be one of"):
        await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "passwords"})
    with pytest.raises(ValueError, match="claim_ref is required"):
        await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "bill"})


# ----------------------------------------------------------------------- read


async def test_read_records(desk: ClaimDeskConnector) -> None:
    medical = await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "medical_records"})
    assert medical["found"] and len(medical["documents"]) == 2
    assert "no history of fracture" in " ".join(d["text"] for d in medical["documents"]).lower()

    policy = await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "policy"})
    assert policy["schedule"]["policy_number"] == "DHI-DEMO-2026-000123"
    assert "Take-home medicines" in policy["documents"][0]["text"]

    bill = await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "bill", "claim_ref": CLAIM})
    assert "33,650" in bill["documents"][0]["text"] and "ocr" not in bill["documents"][0]

    form = await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "hospital_form", "claim_ref": CLAIM})
    assert form["complete"] and form["fields"]["amount_billed"] == "33650" and form["missing_fields"] == []

    absent = await desk.execute_tool(
        "read_record", {"member_id": MEMBER, "record": "hospital_form", "claim_ref": "DEMO-CLAIM-002"}
    )
    assert absent["found"] is False and absent["complete"] is False and absent["documents"] == []


@pytest.mark.skipif(not OCR_READY, reason="Tesseract, Poppler, pytesseract and pdf2image are needed for OCR")
async def test_scanned_bill_is_read_with_ocr(desk: ClaimDeskConnector) -> None:
    bill = await desk.execute_tool(
        "read_record", {"member_id": MEMBER, "record": "bill", "claim_ref": "DEMO-CLAIM-004"}
    )
    (document,) = bill["documents"]
    assert document["readable"] and document["ocr"]["pages"] == [1]
    assert "33,650" in document["text"] and "Take-home medicines" in document["text"]


async def test_scan_without_ocr_tools_is_reported_unreadable(desk: ClaimDeskConnector, monkeypatch) -> None:
    """A machine with no Tesseract (a typical laptop) must say so, not return empty text as if the bill were blank."""
    import core.rag.extractors as extractors

    monkeypatch.setattr(extractors.shutil, "which", lambda _name: None)
    bill = await desk.execute_tool(
        "read_record", {"member_id": MEMBER, "record": "bill", "claim_ref": "DEMO-CLAIM-004"}
    )
    (document,) = bill["documents"]
    assert bill["found"] and document["readable"] is False and document["text"] == ""
    # The wording depends on which OCR piece is missing: Tesseract, pdf2image or Poppler.
    assert any(word in document["reason"].lower() for word in ("ocr", "pdf2image", "poppler"))


async def test_unreadable_file_is_reported_not_guessed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    shutil.copytree(DEMO_DIR / "vault", vault)
    folder = vault / "members" / MEMBER / "claims" / CLAIM
    (folder / "hospital_bill.pdf").write_bytes(b"%PDF-1.4 this is not a real pdf")
    desk = ClaimDeskConnector({"claim_desk_vault_dir": str(vault), "claim_desk_outbox_dir": str(tmp_path / "out")})
    bill = await desk.execute_tool("read_record", {"member_id": MEMBER, "record": "bill", "claim_ref": CLAIM})
    (document,) = bill["documents"]
    assert bill["found"] and document["readable"] is False and document["text"] == "" and document["reason"]


@pytest.mark.parametrize(
    ("member", "claim", "extra", "policy", "form", "source"),
    [
        (MEMBER, CLAIM, {}, "active", "received", "hospital_form"),
        ("DEMO-MEMBER-002", "DEMO-CLAIM-003", {}, "not_active", "received", "hospital_form"),
        (MEMBER, "DEMO-CLAIM-002", {"incident_date": "2026-09-14"}, "active", "missing", "incident_date"),
        (MEMBER, "DEMO-CLAIM-002", {"incident_date": "2020-01-01"}, "not_active", "missing", "incident_date"),
        (
            MEMBER,
            "DEMO-CLAIM-002",
            {"incident_date": "$incident_date", "incident": INCIDENT},
            "active",
            "missing",
            "incident_result",
        ),
    ],
)
async def test_check_status(desk: ClaimDeskConnector, member, claim, extra, policy, form, source) -> None:
    status = await desk.execute_tool("check_status", {"member_id": member, "claim_ref": claim, **extra})
    assert (status["policy_status"], status["hospital_form"], status["date_source"]) == (policy, form, source)
    assert status["bill"] == "present" and status["form_draft"] == "absent"


async def test_incomplete_hospital_form(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    shutil.copytree(DEMO_DIR / "vault", vault)
    template = (DEMO_DIR / "forms" / "claim_form_part_b.pdf").read_bytes()
    pdf, _ = fill_form(template, {"hospital_name": "Sample City Hospital", "date_of_admission": "2026-09-14"})
    (vault / "members" / MEMBER / "claims" / CLAIM / "claim_form_part_b.pdf").write_bytes(pdf)
    desk = ClaimDeskConnector({"claim_desk_vault_dir": str(vault), "claim_desk_outbox_dir": str(tmp_path / "out")})
    await desk.connect()
    status = await desk.execute_tool("check_status", {"member_id": MEMBER, "claim_ref": CLAIM})
    assert status["hospital_form"] == "incomplete"
    assert status["hospital_form_missing_fields"] == [
        "treating_doctor",
        "date_of_discharge",
        "diagnosis",
        "amount_billed",
    ]
    await draft(desk)
    with pytest.raises(ValueError, match="missing or incomplete"):
        await desk.execute_tool("build_filing_payload", {"member_id": MEMBER, "claim_ref": CLAIM, "approval": APPROVED})


# ---------------------------------------------------------------------- draft


async def test_part_a_is_filled_from_documents_and_agent_results(desk: ClaimDeskConnector) -> None:
    result = await draft(desk, incident=INCIDENT, coverage=COVERAGE, transcript={"transcript": "I fell."})
    assert result["status"] == "draft_ready" and result["blank_fields"] == [] and result["warnings"] == []
    fields = read_fields(Path(result["pdf_path"]).read_bytes())
    # from the vault
    assert fields["policy_number"] == "DHI-DEMO-2026-000123" and fields["insured_name"] == "Asha Rao"
    assert fields["bank_ifsc"] == "DEMO0000001"
    # from the hospital's form
    assert fields["date_of_admission"] == "2026-09-14" and fields["amount_billed"] == "33650"
    # from the agent steps
    assert fields["incident_description"].startswith("She slipped") and fields["date_of_event"] == "2026-09-14"
    assert (fields["amount_claimed"], fields["amount_not_claimed"]) == ("31500", "2150")
    assert fields["items_not_claimed"] == "Take-home medicines (INR 1850): clause 4(a)"
    assert result["sha256"] and result["needs_signature"] is True


async def test_signature_is_never_written(desk: ClaimDeskConnector) -> None:
    result = await draft(desk, incident={"insured_signature": "Asha Rao"}, coverage=COVERAGE)
    fields = read_fields(Path(result["pdf_path"]).read_bytes())
    assert all(fields[name] == "" for name in HUMAN_ONLY_FIELDS if name in fields)
    assert "insured_signature" not in result["fields"]


async def test_form_writer_drops_human_only_fields(desk: ClaimDeskConnector) -> None:
    values = {"insured_name": "Asha Rao", "insured_signature": "Asha Rao"}
    result = await desk._write_form(MEMBER, CLAIM, "part_a", values, "claim_form_part_a.pdf", {})
    fields = read_fields(Path(result["pdf_path"]).read_bytes())
    assert fields["insured_name"] == "Asha Rao" and fields["insured_signature"] == ""


async def test_amounts_that_do_not_add_up_are_flagged(desk: ClaimDeskConnector) -> None:
    result = await draft(desk, coverage={"amount_claimed": 30000, "amount_not_claimed": 2150})
    assert any("do not add up" in warning for warning in result["warnings"])


async def test_draft_without_agent_results_or_hospital_form(desk: ClaimDeskConnector) -> None:
    result = await draft(
        desk, claim="DEMO-CLAIM-002", transcript="$transcribe", incident="$incident", coverage="$coverage"
    )
    assert "hospital_name" in result["blank_fields"] and "amount_claimed" in result["blank_fields"]
    assert any("Part B" in warning for warning in result["warnings"])
    assert read_fields(Path(result["pdf_path"]).read_bytes())["insured_name"] == "Asha Rao"


async def test_part_b_request_fills_only_the_patient_section(desk: ClaimDeskConnector) -> None:
    result = await draft(desk, claim="DEMO-CLAIM-002", form="part_b_request")
    assert result["status"] == "hospital_form_requested"
    fields = read_fields(Path(result["pdf_path"]).read_bytes())
    assert fields["patient_name"] == "Asha Rao" and fields["policy_number"] == "DHI-DEMO-2026-000123"
    assert fields["hospital_name"] == "" and fields["diagnosis"] == "" and fields["hospital_signature"] == ""
    with pytest.raises(ValueError, match="form must be"):
        await draft(desk, form="part_c")


# -------------------------------------------------------------------- package


@pytest.mark.parametrize(
    "approval",
    [
        None,
        "$approval",
        {"output": {"decision": "defer"}, "status": "completed"},
        {"decision": "reject"},
        {"decision": ""},
    ],
)
async def test_nothing_is_packaged_without_an_approval(desk: ClaimDeskConnector, approval) -> None:
    await draft(desk, incident=INCIDENT, coverage=COVERAGE)
    with pytest.raises(PermissionError, match="not been approved"):
        await desk.execute_tool("build_filing_payload", {"member_id": MEMBER, "claim_ref": CLAIM, "approval": approval})
    assert not (desk.outbox / MEMBER / CLAIM / "package").exists()


async def test_package_needs_a_draft_and_the_hospital_form(desk: ClaimDeskConnector) -> None:
    with pytest.raises(FileNotFoundError, match="prepare_draft first"):
        await desk.execute_tool("build_filing_payload", {"member_id": MEMBER, "claim_ref": CLAIM, "approval": APPROVED})
    await draft(desk, claim="DEMO-CLAIM-002")
    with pytest.raises(ValueError, match="missing or incomplete"):
        await desk.execute_tool(
            "build_filing_payload", {"member_id": MEMBER, "claim_ref": "DEMO-CLAIM-002", "approval": APPROVED}
        )


async def test_package_contents_receipt_and_idempotence(desk: ClaimDeskConnector) -> None:
    drafted = await draft(desk, incident=INCIDENT, coverage=COVERAGE)
    args = {"member_id": MEMBER, "claim_ref": CLAIM, "approval": APPROVED}
    first = await desk.execute_tool("build_filing_payload", args)
    second = await desk.execute_tool("build_filing_payload", args)

    assert first["status"] == "package_ready"
    assert first["submission"]["mode"] == "simulated" and first["submission"]["confirmation_id"].startswith("SIM-")
    assert first["submission"] == second["submission"]
    assert [f["file"] for f in first["files"]] == [
        "claim_form_part_a.pdf",
        "claim_form_part_b.pdf",
        "hospital_bill.pdf",
    ]
    assert first["files"][0]["sha256"] == drafted["sha256"]
    assert first["warnings"] == ["Part A has no signature in the form field; sign it before uploading"]
    package = Path(first["package_dir"])
    assert sorted(p.name for p in package.iterdir()) == [
        "claim_form_part_a.pdf", "claim_form_part_b.pdf", "hospital_bill.pdf", "manifest.json", "receipt.json",
    ]  # fmt: skip
    assert json.loads((package / "receipt.json").read_text())["mode"] == "simulated"


async def test_package_notes_a_form_changed_after_drafting(desk: ClaimDeskConnector) -> None:
    drafted = await draft(desk, incident=INCIDENT, coverage=COVERAGE)
    path = Path(drafted["pdf_path"])
    signed, _ = fill_form(path.read_bytes(), {"insured_signature": "Asha Rao", "insured_phone": "+91 00000 99999"})
    path.write_bytes(signed)
    result = await desk.execute_tool(
        "build_filing_payload", {"member_id": MEMBER, "claim_ref": CLAIM, "approval": APPROVED}
    )
    assert result["warnings"] == ["Part A was changed after the agent drafted it (for example, signed or corrected)"]
    assert result["files"][0]["sha256"] != drafted["sha256"]


# ------------------------------------------------------------------- platform


async def test_health(desk: ClaimDeskConnector, tmp_path: Path) -> None:
    health = await desk.health_check()
    assert health["status"] == "healthy" and health["members"] == 2 and health["demo_vault"] is True

    empty = ClaimDeskConnector(
        {"claim_desk_vault_dir": str(tmp_path / "nothing"), "claim_desk_outbox_dir": str(tmp_path / "o")}
    )
    unhealthy = await empty.health_check()
    assert unhealthy["status"] == "unhealthy" and "no members" in unhealthy["reason"]

    no_forms = ClaimDeskConnector({"claim_desk_forms_dir": str(tmp_path), "claim_desk_outbox_dir": str(tmp_path / "o")})
    assert "template missing" in (await no_forms.health_check())["reason"]


async def test_another_connectors_base_url_in_merged_config_is_ignored(tmp_path: Path) -> None:
    desk = ClaimDeskConnector({"base_url": "https://gmail.googleapis.com", "claim_desk_outbox_dir": str(tmp_path)})
    await desk.connect()
    assert desk.base_url == "" and desk._client is None
    await desk.disconnect()


def test_plugin_loader_registers_the_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    import connectors  # noqa: F401
    from connectors import plugins
    from connectors.registry import ConnectorRegistry

    monkeypatch.setattr(ConnectorRegistry, "_connectors", dict(ConnectorRegistry._connectors))
    assert ConnectorRegistry.get("claim_desk") is None, "a native connector already uses this name"
    outcomes = plugins.load_plugins(enabled=True, allowlist=plugins.parse_allowlist("agenticorg-claimdesk"))
    assert [(o.loaded, o.reason) for o in outcomes if o.name == "claim_desk"] == [(True, "loaded")]
    assert ConnectorRegistry.get("claim_desk") is ClaimDeskConnector


async def test_every_tool_name_passes_agenticorgs_action_policy() -> None:
    """A tool whose name is not in AgenticOrg's taxonomy is refused in every real run."""
    from core.governance.action_policy import ActionContext, evaluate_action

    tenant, company = str(uuid.uuid4()), str(uuid.uuid4())
    for env in ("development", "production"):
        for tool in ClaimDeskConnector.tools:
            decision = await evaluate_action(
                f"claim_desk:{tool}", context=ActionContext(tenant, company, "operations", env)
            )
            assert decision.dispatch_allowed, (tool, env, decision.reason)
            assert decision.mode.value in ("read-only", "draft")


def test_tools_have_typed_schemas_and_short_descriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    import connectors  # noqa: F401
    from connectors.registry import ConnectorRegistry
    from core.langgraph.tool_adapter import build_tools_for_agent

    monkeypatch.setattr(ConnectorRegistry, "_connectors", dict(ConnectorRegistry._connectors))
    ConnectorRegistry.register(ClaimDeskConnector)
    refs = [f"claim_desk:{tool}" for tool in ClaimDeskConnector.tools]
    tools = build_tools_for_agent(refs, connector_names=["claim_desk"])
    assert sorted(t.name for t in tools) == sorted(f"claim_desk__{tool}" for tool in ClaimDeskConnector.tools)
    for tool in tools:
        assert len(tool.description) <= 1024
        assert "member_id" in tool.args_schema.model_json_schema()["properties"]
