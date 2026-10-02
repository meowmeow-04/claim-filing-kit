"""Claim desk: the non-voice tools a health-insurance claim workflow needs.

It reads the claimant's documents from a vault folder, fills the insured's
claim form (Part A) as a real PDF, checks the hospital's form (Part B), and
assembles the claim package. It makes no network calls.

Tool names are not free choices. AgenticOrg refuses any connector tool whose
name is missing from its action taxonomy (``core/governance/action_policy.py``)
and holds every write in shadow, so the tools here use taxonomy names:

    list_records, read_record, check_status     read
    prepare_draft, build_filing_payload         draft

``build_filing_payload`` therefore stops at a package that is ready to upload.
It does not upload anything; the receipt it writes is marked simulated.

Connector config keys (all optional):

    claim_desk_vault_dir    folder laid out as in vault.py; default: the bundled synthetic demo vault
    claim_desk_outbox_dir   where drafts and packages are written; default: ~/claim-desk-outbox
    claim_desk_forms_dir    folder holding claim_form_part_a.pdf, claim_form_part_b.pdf and an
                            optional field_map.json; default: the bundled demo templates
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog
from connectors.framework.base_connector import BaseConnector

from agenticorg_claimdesk.forms import fill_form, read_fields
from agenticorg_claimdesk.formspec import FORMS, HUMAN_ONLY_FIELDS, PART_B_REQUIRED, field_names
from agenticorg_claimdesk.vault import RECORDS, Vault, safe_id

logger = structlog.get_logger()

DEMO_DIR = Path(__file__).resolve().parent / "demo"
MAX_TEXT_CHARS = 20_000
_AMOUNT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _payload(value: Any) -> dict[str, Any]:
    """Accept a tool/agent result directly, wrapped as a workflow step result, or as JSON text."""
    if isinstance(value, str):
        text = value.strip()
        try:
            value = json.loads(text) if text[:1] in "{[" else {"text": text}
        except ValueError:
            value = {"text": text}
    if not isinstance(value, dict):
        return {}
    if "output" in value and "status" in value:  # a workflow step result
        return _payload(value.get("output"))
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(_text(item) for item in value if _text(item))
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_text(item)}" for key, item in value.items() if _text(item))
    return str(value).strip()


def _amount(value: Any) -> float | None:
    """Parse '31,500', 'INR 31,500.00' or 31500 into a number; None when there is no number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _AMOUNT_RE.search(str(value).replace(",", ""))
    return float(match.group()) if match else None


def _money(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _items_text(value: Any) -> str:
    """'Take-home medicines (INR 1850): excluded, clause 4(a); ...' from a list of {item, amount, reason}."""
    if not isinstance(value, list):
        return _text(value)
    parts = []
    for entry in value:
        if not isinstance(entry, dict):
            parts.append(_text(entry))
            continue
        label = _text(entry.get("item")) or "Item"
        amount = _money(_amount(entry.get("amount")))
        reason = _text(entry.get("reason"))
        parts.append(label + (f" (INR {amount})" if amount else "") + (f": {reason}" if reason else ""))
    return "; ".join(part for part in parts if part)


def _today() -> date:
    return datetime.now().astimezone().date()


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _extract(path: Path) -> dict[str, Any]:
    """Text of one document through AgenticOrg's own extractor (native text first, OCR for scans)."""
    from core.rag.extractors import UnsupportedMimeType, extract  # AgenticOrg, imported in-process

    info: dict[str, Any] = {"file": path.name}
    mime = mimetypes.guess_type(path.name)[0] or ""
    try:
        content = extract(path.read_bytes(), mime, path.name)
    except UnsupportedMimeType as exc:
        return {**info, "readable": False, "reason": str(exc), "text": ""}
    except Exception as exc:  # noqa: BLE001 - one bad file must not hide the others
        return {**info, "readable": False, "reason": f"{type(exc).__name__}: {exc}", "text": ""}
    text = content.full_text()
    extra = content.extra or {}
    info.update(
        readable=bool(text.strip()),
        method=content.extraction_method,
        pages=extra.get("page_count"),
        text=text[:MAX_TEXT_CHARS],
        truncated=len(text) > MAX_TEXT_CHARS,
    )
    if extra.get("ocr_pages"):
        # A diagnostic, not a probability that any figure is right.
        info["ocr"] = {"pages": extra["ocr_pages"], "mean_confidence": extra.get("ocr_mean_confidence")}
    if not info["readable"]:
        info["reason"] = "no text could be read from this file"
    return info


class ClaimDeskConnector(BaseConnector):
    name = "claim_desk"
    category = "ops"
    auth_type = "none"
    base_url = ""
    rate_limit_rpm = 120
    timeout_ms = 120000
    tools = ["list_records", "read_record", "check_status", "prepare_draft", "build_filing_payload"]

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.base_url = ""  # another connector's base_url may be merged into this config; this one has none
        self.vault = Vault(Path(self.config.get("claim_desk_vault_dir") or DEMO_DIR / "vault"))
        self.outbox = Path(self.config.get("claim_desk_outbox_dir") or Path.home() / "claim-desk-outbox").expanduser()
        self.forms_dir = Path(self.config.get("claim_desk_forms_dir") or DEMO_DIR / "forms").expanduser()

    def _register_tools(self) -> None:
        self._tool_registry["list_records"] = self.list_records
        self._tool_registry["read_record"] = self.read_record
        self._tool_registry["check_status"] = self.check_status
        self._tool_registry["prepare_draft"] = self.prepare_draft
        self._tool_registry["build_filing_payload"] = self.build_filing_payload

    def _has_credentials(self) -> bool:
        return True  # nothing to authenticate against

    async def _authenticate(self) -> None:
        """Nothing to do: this connector reads and writes local folders only."""

    async def connect(self) -> None:
        """No HTTP client. Make sure the outbox exists."""
        self.outbox.mkdir(parents=True, exist_ok=True)

    async def health_check(self) -> dict[str, Any]:
        problems: list[str] = []
        members = self.vault.member_ids()
        if not members:
            problems.append(f"no members under {self.vault.root}/members")
        for form, spec in FORMS.items():
            template = self.forms_dir / spec["file"]
            if not template.is_file() or not read_fields(template.read_bytes()):
                problems.append(f"{form} template missing or not fillable: {template}")
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            probe = self.outbox / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"outbox not writable: {exc}")
        try:
            import core.rag.extractors  # noqa: F401
        except ImportError as exc:
            problems.append(f"AgenticOrg document extractor not importable: {exc}")
        if problems:
            return {"status": "unhealthy", "reason": "; ".join(problems)}
        return {
            "status": "healthy",
            "members": len(members),
            "vault": str(self.vault.root),
            "outbox": str(self.outbox),
            "demo_vault": self.vault.root == (DEMO_DIR / "vault").resolve(),
            "ocr_for_scans": "available" if shutil.which("tesseract") and shutil.which("pdftoppm") else "not installed",
        }

    # ------------------------------------------------------------------- read

    async def list_records(self, member_id: str, claim_ref: str = "") -> dict[str, Any]:
        """List what the vault holds for a member, and for one claim when claim_ref is given.

        Returns file names per record type (medical_records, policy, bill,
        hospital_form). Nothing is opened. Use read_record to get the text.
        """
        result: dict[str, Any] = {"member_id": safe_id(member_id, "member_id"), "records": {}}
        for record in ("medical_records", "policy"):
            result["records"][record] = [p.name for p in self.vault.files(member_id, record)]
        if claim_ref:
            result["claim_ref"] = safe_id(claim_ref, "claim_ref")
            for record in ("bill", "hospital_form"):
                result["records"][record] = [p.name for p in self.vault.files(member_id, record, claim_ref)]
        return result

    async def read_record(self, member_id: str, record: str, claim_ref: str = "") -> dict[str, Any]:
        """Read one kind of document from the member's vault and return its text.

        record: medical_records, policy, bill or hospital_form. bill and
        hospital_form also need claim_ref. Scanned pages are read with OCR;
        a file that cannot be read comes back with readable=false and the
        reason, never with guessed text. policy also returns the policy
        schedule; hospital_form also returns the form's fields and which
        required ones the hospital left empty.
        """
        record = str(record or "").strip().lower()
        if record not in RECORDS:
            raise ValueError(f"record must be one of {', '.join(RECORDS)}")
        files = self.vault.files(member_id, record, claim_ref)
        documents = [await asyncio.to_thread(_extract, path) for path in files]
        result: dict[str, Any] = {
            "record": record,
            "member_id": member_id,
            "found": bool(files),
            "documents": documents,
        }
        if claim_ref:
            result["claim_ref"] = claim_ref
        if record == "policy":
            result["schedule"] = self.vault.policy_schedule(member_id)
        if record == "hospital_form":
            fields, missing = self._hospital_form(member_id, claim_ref)
            result.update(fields=fields, missing_fields=missing, complete=bool(files) and not missing)
        return result

    async def check_status(
        self, member_id: str, claim_ref: str, incident_date: str = "", incident: Any = None
    ) -> dict[str, Any]:
        """Deterministic checks on a claim: no model involved.

        Reports whether the policy was in force on the day of the event,
        whether the bill is in the vault, whether the hospital's form is
        received, incomplete or missing, and whether a draft of the insured's
        form exists. The day used is the admission date on the hospital's
        form; without that form, incident_date, then the incident_date in an
        incident result passed in, then today. date_source says which.
        """
        safe_id(claim_ref, "claim_ref")
        schedule = self.vault.policy_schedule(member_id)
        fields, missing = self._hospital_form(member_id, claim_ref)
        has_form = bool(self.vault.files(member_id, "hospital_form", claim_ref))

        on, source = _iso_date(fields.get("date_of_admission")), "hospital_form"
        if on is None:
            on, source = _iso_date(incident_date), "incident_date"
        if on is None:
            on, source = _iso_date(_payload(incident).get("incident_date")), "incident_result"
        if on is None:
            on, source = _today(), "today"
        start, end = _iso_date(schedule.get("period_start")), _iso_date(schedule.get("period_end"))
        if not schedule or start is None or end is None:
            policy_status = "not_found"
        else:
            policy_status = "active" if start <= on <= end else "not_active"

        return {
            "member_id": member_id,
            "claim_ref": claim_ref,
            "policy_status": policy_status,
            "policy_period": f"{schedule.get('period_start', '?')} to {schedule.get('period_end', '?')}",
            "checked_for_date": on.isoformat(),
            "date_source": source,
            "bill": "present" if self.vault.files(member_id, "bill", claim_ref) else "missing",
            "hospital_form": "missing" if not has_form else ("incomplete" if missing else "received"),
            "hospital_form_missing_fields": missing if has_form else [],
            "form_draft": "present" if self._draft_pdf(member_id, claim_ref).is_file() else "absent",
        }

    # ------------------------------------------------------------------ draft

    async def prepare_draft(
        self,
        member_id: str,
        claim_ref: str,
        form: str = "part_a",
        transcript: Any = None,
        incident: Any = None,
        coverage: Any = None,
    ) -> dict[str, Any]:
        """Fill a claim form as a PDF and save it to the outbox. Nothing is sent.

        form=part_a (default): the insured's form. Identity, policy and bank
        fields come from the vault; hospital and bill fields from the
        hospital's form; the incident description and the amounts claimed from
        the transcript, incident and coverage results passed in. The signature
        is always left blank for the insured.
        form=part_b_request: the hospital's form with only the patient section
        filled, to hand to the hospital.
        Returns the PDF path, the values written and the fields still blank.
        """
        form = str(form or "part_a").strip().lower()
        safe_id(claim_ref, "claim_ref")
        profile = self.vault.profile(member_id)
        schedule = self.vault.policy_schedule(member_id)
        if not profile:
            raise LookupError(f"No profile for member {member_id!r} in the vault")
        hospital, _missing = self._hospital_form(member_id, claim_ref)

        common = {
            "policy_number": schedule.get("policy_number"),
            "member_id": profile.get("member_id") or member_id,
            "insurer_name": schedule.get("insurer"),
            "patient_name": profile.get("name"),
            "patient_gender": profile.get("gender"),
            "patient_dob": profile.get("date_of_birth"),
            "insured_phone": profile.get("phone"),
        }
        if form == "part_b_request":
            return await self._write_form(
                member_id, claim_ref, "part_b", common, "claim_form_part_b_for_hospital.pdf", {}
            )
        if form != "part_a":
            raise ValueError("form must be part_a or part_b_request")

        said = _payload(transcript)
        incident_out = _payload(incident)
        coverage_out = _payload(coverage)
        bank = profile.get("bank") if isinstance(profile.get("bank"), dict) else {}

        billed = _amount(hospital.get("amount_billed"))
        if billed is None:
            billed = _amount(coverage_out.get("amount_billed"))
        claimed = _amount(coverage_out.get("amount_claimed"))
        not_claimed = _amount(coverage_out.get("amount_not_claimed"))
        warnings: list[str] = []
        if None not in (billed, claimed, not_claimed) and abs(billed - claimed - not_claimed) > 0.5:
            warnings.append(
                f"amounts do not add up: billed {_money(billed)}, claimed {_money(claimed)}, "
                f"not claimed {_money(not_claimed)}"
            )
        if not self.vault.files(member_id, "hospital_form", claim_ref):
            warnings.append("the hospital's form (Part B) is not in the vault; hospital fields are blank")

        enclosed = [
            label
            for label, record in (
                ("Claim form Part B from the hospital", "hospital_form"),
                ("Final hospital bill", "bill"),
            )
            if self.vault.files(member_id, record, claim_ref)
        ]
        values = {
            **common,
            "date_first_insured": schedule.get("date_first_insured"),
            "insured_name": schedule.get("policy_holder") or profile.get("name"),
            "insured_address": profile.get("address"),
            "insured_city": profile.get("city"),
            "insured_state": profile.get("state"),
            "insured_pin": profile.get("pin"),
            "insured_email": profile.get("email"),
            "other_policy": schedule.get("other_policies"),
            "patient_relationship": schedule.get("relationship"),
            "patient_occupation": profile.get("occupation"),
            "hospital_name": hospital.get("hospital_name"),
            "room_category": hospital.get("room_category"),
            "hospitalisation_due_to": hospital.get("hospitalisation_due_to")
            or _text(incident_out.get("cause")).title(),
            "date_of_event": _text(incident_out.get("incident_date")),
            "medico_legal": hospital.get("medico_legal"),
            "date_of_admission": hospital.get("date_of_admission"),
            "date_of_discharge": hospital.get("date_of_discharge"),
            "diagnosis": hospital.get("diagnosis") or _text(incident_out.get("injury_or_illness")),
            "incident_description": _text(incident_out.get("incident_summary")) or _text(said.get("transcript")),
            "amount_billed": _money(billed),
            "amount_claimed": _money(claimed),
            "amount_not_claimed": _money(not_claimed),
            "items_not_claimed": _items_text(coverage_out.get("not_claimed_items")),
            "documents_enclosed": "; ".join(enclosed),
            "bank_account_holder": bank.get("account_holder"),
            "bank_name": bank.get("bank_name"),
            "bank_account_number": bank.get("account_number"),
            "bank_ifsc": bank.get("ifsc"),
            "declaration_place": profile.get("city"),
            "declaration_date": _today().isoformat(),
        }
        return await self._write_form(
            member_id, claim_ref, "part_a", values, FORMS["part_a"]["file"], {"warnings": warnings}
        )

    async def build_filing_payload(self, member_id: str, claim_ref: str, approval: Any = None) -> dict[str, Any]:
        """Assemble the approved claim package in the outbox. Nothing is uploaded.

        Needs: an approval result whose decision is approve, the filled Part A
        draft, a complete Part B from the hospital and the bill. Copies them
        into a package folder with a manifest of file hashes and writes a
        receipt marked simulated. Calling it again for the same files returns
        the same receipt.
        """
        decision = _text(_payload(approval).get("decision")).lower()
        if decision not in {"approve", "approved"}:
            raise PermissionError("the claim form has not been approved; nothing was packaged")
        part_a = self._draft_pdf(member_id, claim_ref)
        if not part_a.is_file():
            raise FileNotFoundError("no Part A draft in the outbox; run prepare_draft first")
        _fields, missing = self._hospital_form(member_id, claim_ref)
        hospital_files = self.vault.files(member_id, "hospital_form", claim_ref)
        bills = self.vault.files(member_id, "bill", claim_ref)
        if not hospital_files or missing:
            raise ValueError(
                f"the hospital's form (Part B) is missing or incomplete: {', '.join(missing) or 'no file'}"
            )
        if not bills:
            raise ValueError("the hospital bill is not in the vault")
        return await asyncio.to_thread(self._package, member_id, claim_ref, part_a, hospital_files[:1] + bills)

    # ---------------------------------------------------------------- helpers

    def _claim_outbox(self, member_id: str, claim_ref: str) -> Path:
        return self.outbox / safe_id(member_id, "member_id") / safe_id(claim_ref, "claim_ref")

    def _draft_pdf(self, member_id: str, claim_ref: str) -> Path:
        return self._claim_outbox(member_id, claim_ref) / FORMS["part_a"]["file"]

    def _hospital_form(self, member_id: str, claim_ref: str) -> tuple[dict[str, str], list[str]]:
        """Fields of the hospital's form and the required ones left empty."""
        files = [p for p in self.vault.files(member_id, "hospital_form", claim_ref) if p.suffix.lower() == ".pdf"]
        fields = read_fields(files[0].read_bytes()) if files else {}
        missing = [name for name in PART_B_REQUIRED if not fields.get(name)]
        return fields, missing

    def _field_map(self, form: str) -> dict[str, str]:
        path = self.forms_dir / "field_map.json"
        if not path.is_file():
            return {}
        try:
            mapping = json.loads(path.read_text(encoding="utf-8")).get(form, {})
        except (OSError, ValueError, AttributeError):
            return {}
        return {str(k): str(v) for k, v in mapping.items()} if isinstance(mapping, dict) else {}

    async def _write_form(
        self, member_id: str, claim_ref: str, form: str, values: dict[str, Any], filename: str, extra: dict[str, Any]
    ) -> dict[str, Any]:
        values = {k: _text(v) for k, v in values.items() if k not in HUMAN_ONLY_FIELDS}
        template = self.forms_dir / FORMS[form]["file"]
        if not template.is_file():
            raise FileNotFoundError(f"form template not found: {template}")
        pdf, unmapped = await asyncio.to_thread(fill_form, template.read_bytes(), values, self._field_map(form))
        folder = self._claim_outbox(member_id, claim_ref)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / filename
        target.write_bytes(pdf)
        blank = [name for name in field_names(form) if not values.get(name) and name not in HUMAN_ONLY_FIELDS]
        result = {
            "form": form,
            "status": "draft_ready" if filename == FORMS["part_a"]["file"] else "hospital_form_requested",
            "pdf_path": str(target),
            "sha256": _sha256(pdf),
            "fields": {k: v for k, v in values.items() if v},
            "blank_fields": blank if form == "part_a" else [],
            "needs_signature": True,
            "note": (
                "Draft only. The insured must check it and sign before it is sent."
                if form == "part_a"
                else "Give this to the hospital to complete, sign and seal, "
                "then add it to the vault as claim_form_part_b.pdf."
            ),
            **extra,
        }
        if unmapped:
            result["fields_not_in_template"] = unmapped
        (folder / f"{Path(filename).stem}.draft.json").write_text(
            json.dumps({**result, "prepared_at": datetime.now().astimezone().isoformat()}, indent=2), encoding="utf-8"
        )
        logger.info("claim_desk_form_prepared", form=form, claim_ref=claim_ref, blank=len(result["blank_fields"]))
        return result

    def _package(self, member_id: str, claim_ref: str, part_a: Path, attachments: list[Path]) -> dict[str, Any]:
        folder = self._claim_outbox(member_id, claim_ref) / "package"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        files = []
        for source in [part_a, *attachments]:
            data = source.read_bytes()
            (folder / source.name).write_bytes(data)
            files.append({"file": source.name, "bytes": len(data), "sha256": _sha256(data)})

        warnings: list[str] = []
        draft_note = part_a.with_name(f"{part_a.stem}.draft.json")
        try:
            drafted_sha = json.loads(draft_note.read_text(encoding="utf-8")).get("sha256")
        except (OSError, ValueError):
            drafted_sha = None
        if drafted_sha and drafted_sha != files[0]["sha256"]:
            warnings.append("Part A was changed after the agent drafted it (for example, signed or corrected)")
        if not read_fields(part_a.read_bytes()).get("insured_signature"):
            warnings.append("Part A has no signature in the form field; sign it before uploading")

        digest = _sha256("|".join([member_id, claim_ref, *(f["sha256"] for f in files)]).encode())
        receipt = {
            "mode": "simulated",
            "confirmation_id": f"SIM-{digest[:10].upper()}",
            "note": "No upload was made. Upload the package folder to the insurer yourself.",
        }
        manifest = {"member_id": member_id, "claim_ref": claim_ref, "files": files, "warnings": warnings}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (folder / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        logger.info("claim_desk_package_built", claim_ref=claim_ref, files=len(files))
        return {"status": "package_ready", "package_dir": str(folder), "submission": receipt, **manifest}
