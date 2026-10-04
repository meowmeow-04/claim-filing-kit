# ruff: noqa: E501
"""Build a demo vault with one more claim: food poisoning (DEMO-CLAIM-005).

The bundled demo vault holds a wrist-fracture claim. A spoken statement about
another incident does not match its bill and hospital form, so this writes a
separate vault: a copy of the bundled one plus a food-poisoning admission for
DEMO-MEMBER-001, drawn and filled with the same code and layout as the bundled
documents. The bundled demo, and the tests that use it, are left as they are.

    pip install reportlab pypdf          # used here only, not by the connector
    python tools/build_claim_vault.py ~/claim-desk-vault

It keeps one hospital stay per incident (the bundled vault's two test
variants of the wrist-fracture stay are left out). Point the claim desk at
it (claim_desk_vault_dir, or website_claim.py --vault). Everything written is
synthetic. Every page says so.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from reportlab.lib.units import mm

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_demo_assets as demo  # noqa: E402  (same drawing helpers and sample member)

CLAIM_REF = "DEMO-CLAIM-005"
MEMBER_ID = "DEMO-MEMBER-001"
ADMITTED, DISCHARGED = "2026-09-27", "2026-09-29"
BILL_NUMBER = "SCH-005-2026"
DOCTOR = "Dr. K. Sample, MD (Internal Medicine)"

BILL_ROWS = [
    ["Item", "Category", "Amount (INR)"],
    ["Registration and admission fee", "Administrative", "300"],
    ["Emergency consultation", "Consultation", "1,200"],
    ["Physician consultation, 2 visits", "Consultation", "2,400"],
    ["Blood tests: blood count, electrolytes, kidney function", "Diagnostics", "1,800"],
    ["Stool examination and culture", "Diagnostics", "900"],
    ["IV fluids and anti-emetic injections", "Medicines (in-patient)", "4,200"],
    ["Room rent, semi-private, 2 days", "Room", "8,000"],
    ["Nursing charges, 2 days", "Nursing", "1,500"],
    ["Take-home medicines dispensed at discharge", "Pharmacy", "950"],
    ["Total", "", "21,250"],
]
AMOUNT_BILLED = "21250"


def bill_blocks(patient: str) -> list:
    return [
        f"{demo.HOSPITAL} - Final bill",
        f"Bill number: {BILL_NUMBER}. Bill date: {DISCHARGED}. Patient: {patient}. In-patient number: IP-DEMO-5512.",
        f"Date of admission: {ADMITTED}. Date of discharge: {DISCHARGED}. Treating doctor: {DOCTOR}.",
        "Diagnosis: acute gastroenteritis due to food poisoning, with moderate dehydration. "
        "Treatment: intravenous rehydration, anti-emetics, observation.",
        {"rows": BILL_ROWS, "widths": [95 * mm, 40 * mm, 30 * mm]},
        "Amount received in full by card. This bill is a synthetic sample.",
    ]


def part_b_values(profile: dict, policy: dict) -> dict:
    return {
        "patient_name": profile["name"],
        "member_id": profile["member_id"],
        "policy_number": policy["policy_number"],
        "insurer_name": policy["insurer"],
        "patient_gender": profile["gender"],
        "patient_dob": profile["date_of_birth"],
        "insured_phone": profile["phone"],
        "hospital_name": demo.HOSPITAL,
        "hospital_registration": "REG-DEMO-0001",
        "treating_doctor": DOCTOR,
        "doctor_registration": "MC-DEMO-67890",
        "ip_number": "IP-DEMO-5512",
        "date_of_admission": ADMITTED,
        "date_of_discharge": DISCHARGED,
        "admission_type": "Emergency",
        "room_category": "Semi-private",
        "discharge_status": "Discharged to home",
        "diagnosis": "Acute gastroenteritis due to food poisoning, with moderate dehydration",
        "icd10_code": "A05.9",
        "procedure": "Intravenous rehydration and anti-emetic therapy",
        "hospitalisation_due_to": "Illness",
        "injury_cause": "Not applicable",
        "medico_legal": "No",
        "past_history": "No previous gastrointestinal illness. Seasonal allergic rhinitis, not related to this admission.",
        "bill_number": BILL_NUMBER,
        "bill_date": DISCHARGED,
        "amount_billed": AMOUNT_BILLED,
        "hospital_signatory": "Billing Manager (sample)",
        "hospital_declaration_date": DISCHARGED,
        "hospital_signature": "(signed and sealed - synthetic sample)",
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    target = Path(sys.argv[1]).expanduser().resolve()
    if target.exists():
        raise SystemExit(f"{target} exists; choose a new folder or remove it first")
    shutil.copytree(demo.DEMO / "vault", target)
    # The bundled vault holds three variants of one wrist-fracture stay for the
    # tests (complete, no Part B, scanned bill). A demo vault keeps one stay per
    # incident, so a claim can be matched to its stay from the statement alone.
    for duplicate in ("DEMO-CLAIM-002", "DEMO-CLAIM-004"):
        shutil.rmtree(target / "members" / MEMBER_ID / "claims" / duplicate)

    data = demo.MEMBERS[MEMBER_ID]
    folder = target / "members" / MEMBER_ID / "claims" / CLAIM_REF
    folder.mkdir(parents=True)
    demo.document(folder / "hospital_bill.pdf", "Hospital bill", bill_blocks(data["profile"]["name"]))
    template = (demo.DEMO / "forms" / demo.FORMS["part_b"]["file"]).read_bytes()
    filled, missing = demo.fill_form(template, part_b_values(data["profile"], data["policy"]))
    assert not missing, missing
    (folder / "claim_form_part_b.pdf").write_bytes(filled)
    for path in sorted(folder.iterdir()):
        print(f"{path.stat().st_size:>8}  {path.relative_to(target)}")
    print(f"Vault ready: {target}")


if __name__ == "__main__":
    main()
