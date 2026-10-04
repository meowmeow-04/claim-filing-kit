"""Assemble the workflow definition from the step list below and the prompt files.

    python3 workflow/build_workflow.py

Writes workflow/health_claim_filing.json: the ``definition`` AgenticOrg's
``POST /api/v1/workflows`` expects. Edit a prompt in workflow/prompts, or a
step here, then run this again. Standard library only.

How data moves between steps (AgenticOrg's engine, as of commit 94c62a3):
- a connector step's ``params`` value ``"$name"`` is replaced by the run
  payload value, or the whole result of the earlier step, called ``name``;
  when there is neither, the text ``"$name"`` is passed on as it is;
- an agent step receives the payload and every earlier step result as
  ``task.context``; ``{{...}}`` placeholders are NOT filled in;
- a condition reads ``<step id>.output.<field>``.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMON = (HERE / "prompts" / "_common.md").read_text(encoding="utf-8").strip()

CLAIM = {"member_id": "$member_id", "claim_ref": "$claim_ref"}


def connector(step_id: str, state: str, connector_name: str, tool: str, params: dict, after: list[str]) -> dict:
    return {
        "id": step_id,
        "name": state,
        "type": "connector_tool",
        "connector": connector_name,
        "tool": tool,
        "params": params,
        "depends_on": after,
    }


def agent(step_id: str, state: str, action: str, prompt: str, after: list[str]) -> dict:
    specific = (HERE / "prompts" / f"{prompt}.md").read_text(encoding="utf-8").strip()
    return {
        "id": step_id,
        "name": state,
        "type": "agent",
        # No built-in agent class has this name, so AgenticOrg runs a plain agent with the prompt below.
        "agent_type": "claim_analyst",
        "action": action,
        "inputs": {"instruction": f"Do the '{action}' job described in your instructions."},
        "authorized_tools": [],  # the analyst reasons over what earlier steps fetched; it calls nothing
        "system_prompt_text": f"{COMMON}\n\n{specific}",
        "depends_on": after,
    }


STEPS = [
    # State 1 - transcription
    # A run carries either a statement captured live (voice_session_id and its text,
    # voice_transcript) or a recorded clip (audio_url). The connector ignores the
    # placeholders the run did not fill.
    connector(
        "transcribe",
        "State 1: transcription of the spoken statement",
        "gnani_stt",
        "query",
        {
            "voice_session_id": "$voice_session_id",
            "voice_transcript": "$voice_transcript",
            "audio_url": "$audio_url",
            "language_code": "$language_code",
        },
        [],
    ),
    # State 2 - understand the incident
    connector(
        "medical_records",
        "State 2: fetch medical reports from the vault",
        "claim_desk",
        "read_record",
        {**CLAIM, "record": "medical_records"},
        ["transcribe"],
    ),
    agent("incident", "State 2: understand the incident", "assess_incident", "assess_incident", ["medical_records"]),
    # State 2 - clarification. When the analyst is unsure, the run pauses on
    # ``incident`` and the insured answers its open questions in the approval
    # note (scripts/register.py answer). The review below then corrects the
    # assessment with those answers. The engine only runs forward, so there is
    # one round of questions; a review that is still unsure pauses for a person.
    # Later steps depend on the gate, not the review: a skipped review would
    # otherwise skip them too. When the gate passes, the review runs before
    # ``policy`` because ties go by definition order (tests/test_workflow.py).
    {
        "id": "clarify_gate",
        "name": "State 2: did the insured answer questions about the incident?",
        "type": "condition",
        "condition": 'incident.output.hitl_decision.notes != ""',
        "true_path": "incident_review",
        "depends_on": ["incident"],
    },
    agent(
        "incident_review",
        "State 2: correct the incident with the insured's answers",
        "review_incident",
        "review_incident",
        ["clarify_gate"],
    ),
    # State 3 - claim eligibility
    connector(
        "policy",
        "State 3: fetch the policy from the vault",
        "claim_desk",
        "read_record",
        {**CLAIM, "record": "policy"},
        ["clarify_gate"],
    ),
    connector(
        "checks",
        "State 3: check policy dates and documents",
        "claim_desk",
        "check_status",
        {**CLAIM, "incident_date": "$incident_date", "incident": "$incident", "incident_review": "$incident_review"},
        ["policy"],
    ),
    agent("eligibility", "State 3: check eligibility", "check_eligibility", "check_eligibility", ["checks"]),
    {
        "id": "eligibility_gate",
        "name": "State 3: stop unless the policy is in force and the incident may be covered",
        "type": "condition",
        "condition": (
            'checks.output.policy_status == "active" and eligibility.output.decision in ["covered", "needs_review"]'
        ),
        "true_path": "bill",
        "false_path": "stop_not_eligible",
        "depends_on": ["eligibility"],
    },
    {
        "id": "stop_not_eligible",
        "name": "Stopped: no claim prepared",
        "type": "transform",
        "operation": "pick",
        "fields": ["checks", "eligibility"],
        "depends_on": ["eligibility_gate"],
    },
    # State 4 - coverage summary
    connector(
        "bill",
        "State 4: read the hospital bill (OCR for scans)",
        "claim_desk",
        "read_record",
        {**CLAIM, "record": "bill"},
        ["eligibility_gate"],
    ),
    connector(
        "hospital_form",
        "State 4: read the hospital's form (Part B)",
        "claim_desk",
        "read_record",
        {**CLAIM, "record": "hospital_form"},
        ["bill"],
    ),
    agent("coverage", "State 4: coverage summary", "summarise_coverage", "summarise_coverage", ["hospital_form"]),
    # State 5 - incident report
    agent("report", "State 5: incident report", "write_report", "write_report", ["coverage"]),
    # State 5 - answers given after the incident was assessed. Eligibility,
    # coverage or the report can pause with questions too; their answers do not
    # re-run that step (the engine only runs forward), but this review brings
    # the incident facts up to date before the form is filled, so a year given
    # at eligibility reaches the form. fill_form follows the gate; the review
    # runs first by definition order (tests/test_workflow.py).
    {
        "id": "answers_gate",
        "name": "State 5: did the insured answer questions after the incident step?",
        "type": "condition",
        "condition": (
            'eligibility.output.hitl_decision.notes != "" or coverage.output.hitl_decision.notes != "" '
            'or report.output.hitl_decision.notes != ""'
        ),
        "true_path": "facts_review",
        "depends_on": ["report"],
    },
    agent(
        "facts_review",
        "State 5: bring the incident facts up to date with the insured's answers",
        "review_facts",
        "review_facts",
        ["answers_gate"],
    ),
    # State 6 - fill the insured's form
    connector(
        "fill_form",
        "State 6: fill the insured's form (Part A) as a PDF",
        "claim_desk",
        "prepare_draft",
        {
            **CLAIM,
            "form": "part_a",
            "transcript": "$transcribe",
            "incident": "$incident",
            "incident_review": "$incident_review",
            "facts_review": "$facts_review",
            "coverage": "$coverage",
        },
        ["answers_gate"],
    ),
    {
        "id": "hospital_gate",
        "name": "State 6: is the hospital's form in?",
        "type": "condition",
        "condition": 'checks.output.hospital_form == "received"',
        "true_path": "approval",
        "false_path": "hospital_form_needed",
        "depends_on": ["fill_form"],
    },
    connector(
        "hospital_form_needed",
        "Stopped: Part B prepared for the hospital to complete",
        "claim_desk",
        "prepare_draft",
        {**CLAIM, "form": "part_b_request"},
        ["hospital_gate"],
    ),
    # State 7 - approval by the insured
    {
        "id": "approval",
        "name": "State 7: the insured approves the form",
        "title": "Check and approve the claim form before it is packaged",
        "type": "human_in_loop",
        "assignee_role": "admin",
        # AgenticOrg's own shape; a plain list made a reject crash with a 500.
        "decision_options": {"options": ["approve", "reject"]},
        "timeout_hours": 48,
        "depends_on": ["hospital_gate"],
    },
    # State 8 - claim package (the upload itself stays with a person)
    connector(
        "package",
        "State 8: build the claim package",
        "claim_desk",
        "build_filing_payload",
        {**CLAIM, "approval": "$approval"},
        ["approval"],
    ),
]

DEFINITION = {
    "name": "health_claim_filing",
    "version": "1.5",
    "description": (
        "Prepares a health-insurance reimbursement claim for the insured: transcribes their account, reads their "
        "documents, estimates coverage, fills Part A as a PDF, checks the hospital's Part B, waits for the "
        "insured's approval and builds the claim package. It does not upload anything to an insurer."
    ),
    "domain": "operations",
    "trigger": {"type": "manual"},
    "timeout_hours": 72,
    "steps": STEPS,
}

if __name__ == "__main__":
    target = HERE / "health_claim_filing.json"
    target.write_text(json.dumps(DEFINITION, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {target.name}: {len(STEPS)} steps")
