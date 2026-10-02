"""The two claim forms, as data.

A reimbursement claim in Indian health insurance is two forms: Part A, filled
by the insured, and Part B, filled by the hospital. The templates shipped in
``demo/forms`` are generic demo forms modelled on that split. They are not any
insurer's form.

Each section is ``(title, rows)``; each row is a list of
``(label, field_name, relative_width)``. A fourth item sets the row height in
points for multi-line fields. The same spec draws the fillable PDF templates
(``tools/build_demo_assets.py``) and tells the connector which fields exist.
"""

from __future__ import annotations

Row = list[tuple]
Section = tuple[str, list[Row]]

PART_A: list[Section] = [
    (
        "A. Primary insured",
        [
            [("Policy number", "policy_number", 2), ("Member ID", "member_id", 1)],
            [("Insurer", "insurer_name", 2), ("Date first insured", "date_first_insured", 1)],
            [("Name of policy holder", "insured_name", 2), ("Phone", "insured_phone", 1)],
            [("Address", "insured_address", 3)],
            [("City", "insured_city", 1), ("State", "insured_state", 1), ("PIN", "insured_pin", 1)],
            [("Email", "insured_email", 2), ("Covered by another policy", "other_policy", 1)],
        ],
    ),
    (
        "B. Insured person hospitalised",
        [
            [("Name of patient", "patient_name", 2), ("Relationship to policy holder", "patient_relationship", 1)],
            [
                ("Gender", "patient_gender", 1),
                ("Date of birth", "patient_dob", 1),
                ("Occupation", "patient_occupation", 1),
            ],
        ],
    ),
    (
        "C. Hospitalisation",
        [
            [("Hospital", "hospital_name", 2), ("Room category", "room_category", 1)],
            [
                ("Due to (injury / illness)", "hospitalisation_due_to", 1),
                ("Date of injury or first symptoms", "date_of_event", 1),
                ("Medico-legal case", "medico_legal", 1),
            ],
            [
                ("Date of admission", "date_of_admission", 1),
                ("Date of discharge", "date_of_discharge", 1),
                ("Diagnosis", "diagnosis", 1),
            ],
            [("How it happened, as described by the insured", "incident_description", 3, 74)],
        ],
    ),
    (
        "D. Claim",
        [
            [
                ("Hospital bill total (INR)", "amount_billed", 1),
                ("Amount claimed (INR)", "amount_claimed", 1),
                ("Not claimed (INR)", "amount_not_claimed", 1),
            ],
            [("Items not claimed and why", "items_not_claimed", 3, 40)],
            [("Documents enclosed", "documents_enclosed", 3, 40)],
        ],
    ),
    (
        "E. Bank account for reimbursement",
        [
            [("Account holder", "bank_account_holder", 2), ("Bank", "bank_name", 1)],
            [("Account number", "bank_account_number", 2), ("IFSC", "bank_ifsc", 1)],
        ],
    ),
    (
        "F. Declaration by the insured (sign before sending)",
        [
            [
                ("Place", "declaration_place", 1),
                ("Date", "declaration_date", 1),
                ("Signature of the insured", "insured_signature", 1),
            ],
        ],
    ),
]

PART_B: list[Section] = [
    (
        "A. Patient (may be pre-filled by the insured)",
        [
            [("Name of patient", "patient_name", 2), ("Member ID", "member_id", 1)],
            [("Policy number", "policy_number", 2), ("Insurer", "insurer_name", 1)],
            [("Gender", "patient_gender", 1), ("Date of birth", "patient_dob", 1), ("Phone", "insured_phone", 1)],
        ],
    ),
    (
        "B. Hospital",
        [
            [("Hospital", "hospital_name", 2), ("Registration number", "hospital_registration", 1)],
            [("Treating doctor", "treating_doctor", 2), ("Doctor's registration number", "doctor_registration", 1)],
        ],
    ),
    (
        "C. Admission",
        [
            [
                ("In-patient number", "ip_number", 1),
                ("Date of admission", "date_of_admission", 1),
                ("Date of discharge", "date_of_discharge", 1),
            ],
            [
                ("Type of admission", "admission_type", 1),
                ("Room category", "room_category", 1),
                ("Status at discharge", "discharge_status", 1),
            ],
        ],
    ),
    (
        "D. Diagnosis and treatment",
        [
            [("Primary diagnosis", "diagnosis", 2), ("ICD-10 code", "icd10_code", 1)],
            [("Procedure", "procedure", 2), ("Due to (injury / illness)", "hospitalisation_due_to", 1)],
            [("Cause of injury", "injury_cause", 2), ("Medico-legal case", "medico_legal", 1)],
            [("Relevant past history, as recorded by the hospital", "past_history", 3, 40)],
        ],
    ),
    (
        "E. Bill",
        [
            [
                ("Bill number", "bill_number", 1),
                ("Bill date", "bill_date", 1),
                ("Total bill amount (INR)", "amount_billed", 1),
            ],
        ],
    ),
    (
        "F. Declaration by the hospital",
        [
            [("Name and designation", "hospital_signatory", 2), ("Date", "hospital_declaration_date", 1)],
            [("Signature and hospital seal", "hospital_signature", 3, 40)],
        ],
    ),
]

FORMS: dict[str, dict] = {
    "part_a": {
        "title": "Health Insurance Claim Form - Part A",
        "subtitle": "To be filled by the insured",
        "file": "claim_form_part_a.pdf",
        "sections": PART_A,
    },
    "part_b": {
        "title": "Health Insurance Claim Form - Part B",
        "subtitle": "To be filled by the hospital",
        "file": "claim_form_part_b.pdf",
        "sections": PART_B,
    },
}

# Fields only a person may complete. The connector never writes these.
HUMAN_ONLY_FIELDS = frozenset(
    {"insured_signature", "hospital_signature", "hospital_signatory", "hospital_declaration_date"}
)

# Part B fields the hospital must complete for the form to count as received.
PART_B_REQUIRED = (
    "hospital_name",
    "treating_doctor",
    "date_of_admission",
    "date_of_discharge",
    "diagnosis",
    "amount_billed",
)


def field_names(form: str) -> list[str]:
    return [cell[1] for _title, rows in FORMS[form]["sections"] for row in rows for cell in row]
