# ruff: noqa: E501, S607
"""Rebuild the demo form templates and the synthetic demo vault.

Only needed when you change the forms or the sample data; the generated files
are already in ``src/agenticorg_claimdesk/demo``.

    pip install reportlab          # used here only, not by the connector
    python tools/build_demo_assets.py

Everything written is synthetic. Every page says so.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agenticorg_claimdesk.forms import fill_form  # noqa: E402
from agenticorg_claimdesk.formspec import FORMS  # noqa: E402

DEMO = ROOT / "src" / "agenticorg_claimdesk" / "demo"
PAGE_W, PAGE_H = A4
MARGIN = 34
INK = colors.HexColor("#1c2733")
RULE = colors.HexColor("#8a97a6")
BAND = colors.HexColor("#e8edf2")


# ------------------------------------------------------------------ templates


def draw_form(form: str, path: Path) -> None:
    spec = FORMS[form]
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"{spec['title']} (demo template)")
    c.setAuthor("agenticorg-claimdesk demo")
    usable = PAGE_W - 2 * MARGIN
    y = PAGE_H - MARGIN

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(MARGIN, y - 12, spec["title"])
    c.setFont("Helvetica", 9)
    c.drawString(MARGIN, y - 26, spec["subtitle"])
    c.setFillColor(colors.HexColor("#9a3b2e"))
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(PAGE_W - MARGIN, y - 12, "DEMO TEMPLATE")
    c.setFont("Helvetica", 7)
    c.drawRightString(PAGE_W - MARGIN, y - 23, "Generic sample layout. Not any insurer's form.")
    y -= 38

    form_fields = c.acroForm
    for title, rows in spec["sections"]:
        c.setFillColor(BAND)
        c.rect(MARGIN, y - 13, usable, 13, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(MARGIN + 4, y - 9.5, title)
        y -= 17
        for row in rows:
            height = max((cell[3] if len(cell) > 3 else 14) for cell in row)
            total = sum(cell[2] for cell in row)
            gap = 6
            x = MARGIN
            for cell in row:
                label, name, weight = cell[0], cell[1], cell[2]
                width = (usable - gap * (len(row) - 1)) * weight / total
                c.setFillColor(INK)
                c.setFont("Helvetica", 6.8)
                c.drawString(x + 1, y - 6.5, label)
                form_fields.textfield(
                    name=name,
                    tooltip=label,
                    x=x,
                    y=y - 8.5 - height,
                    width=width,
                    height=height,
                    fontName="Helvetica",
                    fontSize=8.5,
                    borderWidth=0.5,
                    borderColor=RULE,
                    fillColor=colors.white,
                    textColor=colors.black,
                    forceBorder=True,
                    maxlen=4000,
                    fieldFlags="multiline" if height > 14 else "",
                )
                x += width + gap
            y -= 8.5 + height + 3.5
        y -= 2

    c.setFillColor(RULE)
    c.setFont("Helvetica", 6.5)
    c.drawString(MARGIN, MARGIN - 12, f"agenticorg-claimdesk demo template - {spec['file']}")
    c.save()


# ------------------------------------------------------------------ documents

STYLES = getSampleStyleSheet()
BODY = STYLES["BodyText"]
BODY.fontSize = 9.5
BODY.leading = 13


def _watermark(c: canvas.Canvas, _doc: object) -> None:
    c.saveState()
    c.setFont("Helvetica-Bold", 30)
    c.setFillColor(colors.HexColor("#e2e6ea"))
    c.translate(PAGE_W / 2, PAGE_H / 2)
    c.rotate(38)
    c.drawCentredString(0, 0, "SYNTHETIC SAMPLE")
    c.restoreState()
    c.setFont("Helvetica", 7)
    c.setFillColor(RULE)
    c.drawString(
        20 * mm,
        12 * mm,
        "Synthetic sample for software testing. Not a real record. All names and numbers are invented.",
    )


def document(path: Path, title: str, blocks: list) -> None:
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title=f"{title} (synthetic sample)",
        author="agenticorg-claimdesk demo",
    )
    story: list = [Paragraph(title, STYLES["Title"]), Spacer(1, 4)]
    for block in blocks:
        if isinstance(block, str):
            story.append(Paragraph(block, BODY))
            story.append(Spacer(1, 5))
        else:
            table = Table(block["rows"], colWidths=block.get("widths"), hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
                        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
                        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(table)
            story.append(Spacer(1, 8))
    doc.build(story, onFirstPage=_watermark, onLaterPages=_watermark)


def scanned_copy(source_pdf: Path, target_pdf: Path) -> None:
    """Rasterise a PDF into an image-only PDF, so reading it needs OCR."""
    if not shutil.which("pdftoppm"):
        raise SystemExit("pdftoppm (Poppler) is needed to build the scanned bill")
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run(  # noqa: S603
            ["pdftoppm", "-r", "150", "-gray", "-png", "-singlefile", str(source_pdf), f"{folder}/page"], check=True
        )
        c = canvas.Canvas(str(target_pdf), pagesize=A4)
        c.setTitle("Hospital bill - scanned copy (synthetic sample)")
        c.drawImage(f"{folder}/page.png", 0, 0, width=PAGE_W, height=PAGE_H)
        c.save()


# ----------------------------------------------------------------------- data

INSURER = "Demo Health Insurance Co. (fictional)"
HOSPITAL = "Sample City Hospital (fictional)"

MEMBERS = {
    "DEMO-MEMBER-001": {
        "profile": {
            "member_id": "DEMO-MEMBER-001",
            "name": "Asha Rao",
            "gender": "Female",
            "date_of_birth": "1994-05-12",
            "occupation": "Graphic designer",
            "address": "14 Sample Residency, MG Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pin": "560001",
            "phone": "+91 00000 00001",
            "email": "asha.rao@example.com",
            "bank": {
                "account_holder": "Asha Rao",
                "bank_name": "Demo Bank (fictional)",
                "account_number": "000000001234",
                "ifsc": "DEMO0000001",
            },
            "synthetic": True,
        },
        "policy": {
            "insurer": INSURER,
            "policy_number": "DHI-DEMO-2026-000123",
            "product": "Demo Individual Health Plan",
            "policy_holder": "Asha Rao",
            "relationship": "Self",
            "period_start": "2026-04-01",
            "period_end": "2027-03-31",
            "date_first_insured": "2023-04-01",
            "sum_insured_inr": 500000,
            "room_rent_limit_per_day_inr": 5000,
            "pre_existing_waiting_period_months": 36,
            "other_policies": "No",
            "synthetic": True,
        },
    },
    "DEMO-MEMBER-002": {
        "profile": {
            "member_id": "DEMO-MEMBER-002",
            "name": "Kabir Mehta",
            "gender": "Male",
            "date_of_birth": "1988-11-03",
            "occupation": "School teacher",
            "address": "7 Example Enclave, Park Street",
            "city": "Pune",
            "state": "Maharashtra",
            "pin": "411001",
            "phone": "+91 00000 00002",
            "email": "kabir.mehta@example.com",
            "bank": {
                "account_holder": "Kabir Mehta",
                "bank_name": "Demo Bank (fictional)",
                "account_number": "000000005678",
                "ifsc": "DEMO0000002",
            },
            "synthetic": True,
        },
        "policy": {
            "insurer": INSURER,
            "policy_number": "DHI-DEMO-2025-000456",
            "product": "Demo Individual Health Plan",
            "policy_holder": "Kabir Mehta",
            "relationship": "Self",
            "period_start": "2025-04-01",
            "period_end": "2026-03-31",
            "date_first_insured": "2025-04-01",
            "sum_insured_inr": 300000,
            "room_rent_limit_per_day_inr": 3000,
            "pre_existing_waiting_period_months": 36,
            "other_policies": "No",
            "synthetic": True,
        },
    },
}

POLICY_WORDING = [
    "<b>Demo Individual Health Plan - policy wording (synthetic sample)</b>",
    "<b>1. What is covered.</b> In-patient hospitalisation of 24 hours or more, and listed day-care procedures. "
    "Payable items are doctor and specialist consultation fees, diagnostic tests and imaging, procedure and operating "
    "theatre charges, anaesthesia, and room rent up to the daily limit in the schedule.",
    "<b>2. Accidents.</b> Hospitalisation for an injury caused by an accident is covered from the first day of the policy. "
    "No waiting period applies to accidental injury.",
    "<b>3. Waiting periods.</b> Illness in the first 30 days of the first policy is not covered. A pre-existing disease, "
    "and any complication arising from it, is covered only after 36 months of continuous cover.",
    "<b>4. What is not covered.</b> (a) Take-home medicines and outpatient pharmacy items dispensed at or after discharge. "
    "(b) Registration, admission and other administrative charges. (c) Non-medical consumables and convenience items. "
    "(d) Treatment received outside the policy period.",
    "<b>5. Reimbursement claims.</b> Send the claim within 30 days of discharge: Part A completed and signed by the "
    "insured, Part B completed, signed and sealed by the hospital, the final hospital bill with receipts, and the "
    "discharge summary.",
    "<b>6. Who decides.</b> The insurer assesses every claim against this wording and the documents received. "
    "Nothing prepared by the insured or by any software on the insured's behalf is a decision on the claim.",
]

MEDICAL_001 = {
    "2025-11-02_annual_health_check.pdf": (
        "Annual health check - 2 November 2025",
        [
            "Patient: Asha Rao, female, 31 years. Clinic: Example Wellness Clinic (fictional).",
            "Blood pressure 118/76 mmHg. Fasting glucose 88 mg/dL. HbA1c 5.2%. Lipid profile within normal limits. "
            "Body mass index 22.4.",
            "Known conditions: seasonal allergic rhinitis since 2019, managed with antihistamines as needed.",
            "Musculoskeletal: no history of fracture, bone disease, osteoporosis or joint disorder. Vitamin D 34 ng/mL.",
            "Impression: healthy adult. No further investigation advised.",
        ],
    ),
    "2024-08-19_physician_note.pdf": (
        "Physician consultation note - 19 August 2024",
        [
            "Patient: Asha Rao, female, 30 years. Clinic: Example Wellness Clinic (fictional).",
            "Complaint: sneezing and nasal congestion for one week. Diagnosis: seasonal allergic rhinitis.",
            "Plan: cetirizine 10 mg at night for 10 days. Review if symptoms persist.",
        ],
    ),
}

MEDICAL_002 = {
    "2025-06-10_physician_note.pdf": (
        "Physician consultation note - 10 June 2025",
        [
            "Patient: Kabir Mehta, male, 36 years. Clinic: Example Family Clinic (fictional).",
            "Routine visit. No chronic conditions recorded. No regular medication.",
        ],
    ),
}

BILL_ROWS = [
    ["Item", "Category", "Amount (INR)"],
    ["Registration and admission fee", "Administrative", "300"],
    ["Emergency consultation", "Consultation", "1,200"],
    ["Orthopaedic specialist consultation", "Consultation", "1,500"],
    ["X-ray, right wrist, two views", "Diagnostics", "900"],
    ["Pre-procedure blood tests", "Diagnostics", "1,400"],
    ["Room rent, semi-private, 1 day", "Room", "4,000"],
    ["Closed reduction and casting, with theatre charges", "Procedure", "18,000"],
    ["Anaesthesia", "Procedure", "4,500"],
    ["Take-home medicines dispensed at discharge", "Pharmacy", "1,850"],
    ["Total", "", "33,650"],
]


def bill_blocks(patient: str, bill_number: str, admitted: str, discharged: str) -> list:
    return [
        f"{HOSPITAL} - Final bill",
        f"Bill number: {bill_number}. Bill date: {discharged}. Patient: {patient}. In-patient number: IP-DEMO-4471.",
        f"Date of admission: {admitted}. Date of discharge: {discharged}. Treating doctor: Dr. R. Sample, Orthopaedics.",
        "Diagnosis: closed fracture of the distal end of the right radius. Procedure: closed reduction and below-elbow cast.",
        {"rows": BILL_ROWS, "widths": [95 * mm, 40 * mm, 30 * mm]},
        "Amount received in full by card. This bill is a synthetic sample.",
    ]


def part_b_values(profile: dict, policy: dict, bill_number: str, admitted: str, discharged: str) -> dict:
    return {
        "patient_name": profile["name"],
        "member_id": profile["member_id"],
        "policy_number": policy["policy_number"],
        "insurer_name": policy["insurer"],
        "patient_gender": profile["gender"],
        "patient_dob": profile["date_of_birth"],
        "insured_phone": profile["phone"],
        "hospital_name": HOSPITAL,
        "hospital_registration": "REG-DEMO-0001",
        "treating_doctor": "Dr. R. Sample, MS (Orthopaedics)",
        "doctor_registration": "MC-DEMO-12345",
        "ip_number": "IP-DEMO-4471",
        "date_of_admission": admitted,
        "date_of_discharge": discharged,
        "admission_type": "Emergency",
        "room_category": "Semi-private",
        "discharge_status": "Discharged to home",
        "diagnosis": "Closed fracture of distal end of right radius",
        "icd10_code": "S52.50",
        "procedure": "Closed reduction and below-elbow cast under anaesthesia",
        "hospitalisation_due_to": "Injury",
        "injury_cause": "Accidental fall on stairs at home",
        "medico_legal": "No",
        "past_history": "No previous fracture or bone disease. Seasonal allergic rhinitis, not related to this admission.",
        "bill_number": bill_number,
        "bill_date": discharged,
        "amount_billed": "33650",
        "hospital_signatory": "Billing Manager (sample)",
        "hospital_declaration_date": discharged,
        "hospital_signature": "(signed and sealed - synthetic sample)",
    }


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    forms_dir = DEMO / "forms"
    forms_dir.mkdir(parents=True)
    for form, spec in FORMS.items():
        draw_form(form, forms_dir / spec["file"])
    part_b_template = (forms_dir / FORMS["part_b"]["file"]).read_bytes()

    for member_id, data in MEMBERS.items():
        base = DEMO / "vault" / "members" / member_id
        (base / "policy").mkdir(parents=True)
        (base / "medical").mkdir()
        write_json(base / "profile.json", data["profile"])
        write_json(base / "policy" / "policy.json", data["policy"])
        document(base / "policy" / "policy_wording.pdf", "Policy wording", POLICY_WORDING)
        for name, (title, blocks) in (MEDICAL_001 if member_id.endswith("001") else MEDICAL_002).items():
            document(base / "medical" / name, title, blocks)

    def claim(
        member_id: str, claim_ref: str, admitted: str, discharged: str, *, part_b: bool, scan: bool = False
    ) -> None:
        data = MEMBERS[member_id]
        folder = DEMO / "vault" / "members" / member_id / "claims" / claim_ref
        folder.mkdir(parents=True)
        bill_number = f"SCH-{claim_ref[-3:]}-2026"
        bill = folder / "hospital_bill.pdf"
        document(bill, "Hospital bill", bill_blocks(data["profile"]["name"], bill_number, admitted, discharged))
        if scan:
            scanned = folder / "hospital_bill_scan.pdf"
            scanned_copy(bill, scanned)
            bill.unlink()
        if part_b:
            filled, missing = fill_form(
                part_b_template, part_b_values(data["profile"], data["policy"], bill_number, admitted, discharged)
            )
            assert not missing, missing
            (folder / "claim_form_part_b.pdf").write_bytes(filled)

    claim("DEMO-MEMBER-001", "DEMO-CLAIM-001", "2026-09-14", "2026-09-15", part_b=True)
    claim("DEMO-MEMBER-001", "DEMO-CLAIM-002", "2026-09-14", "2026-09-15", part_b=False)
    claim("DEMO-MEMBER-001", "DEMO-CLAIM-004", "2026-09-14", "2026-09-15", part_b=True, scan=True)
    claim("DEMO-MEMBER-002", "DEMO-CLAIM-003", "2026-09-14", "2026-09-15", part_b=True)  # policy ended 2026-03-31

    (DEMO / "README.txt").write_text(
        "Everything in this folder is synthetic sample data for testing.\n"
        "The form templates are generic demo layouts, not any insurer's form.\n"
        "Rebuild with tools/build_demo_assets.py.\n",
        encoding="utf-8",
    )
    for path in sorted(DEMO.rglob("*")):
        if path.is_file():
            print(f"{path.stat().st_size:>8}  {path.relative_to(DEMO)}")


if __name__ == "__main__":
    main()
