Your job in this step: write the incident report that goes with the claim.

Read the earlier results in `task.context`: `incident.output`, `eligibility.output`, `coverage.output`, `checks.output`, and `hospital_form.output` for whether the hospital's form has been received.

Return these fields
- `report`: plain text, under 250 words, in this order: what happened and when; the treatment and the hospital; the documents on file; the expected coverage and the amount claimed; what is not claimed and why; anything still open.
- `summary_for_approver`: three to five short lines the insured reads before approving the form: the amount claimed, the amount not claimed and why, what is still missing, and that they must sign the form.
- `open_questions`: every open question from the earlier steps, without duplicates.

Report only what the earlier steps found. Add nothing new.
