Your job in this step: go through the hospital bill line by line and say what the policy is expected to pay.

Read
- `task.context.bill.output.documents`: the bill text. If a document has `ocr`, it was a scan read by OCR: digits may be wrong, and `mean_confidence` is only a rough signal. If `readable` is false, you have no bill; say so and set `confidence` to 0.
- `task.context.policy.output.documents`: the policy wording.
- `task.context.hospital_form.output.fields`: the hospital's form, when it has been received. `amount_billed` there is the hospital's own total.

Return these fields
- `line_items`: a list of `{"item", "amount", "category", "expected", "clause", "reason"}`. `expected` is "payable", "not_payable" or "unclear". Copy each amount exactly as printed, as a number.
- `amount_billed`: the bill total as printed.
- `amount_claimed`: the sum of the "payable" and "unclear" items. Unclear items are claimed; the insurer decides them.
- `amount_not_claimed`: the sum of the "not_payable" items.
- `not_claimed_items`: a list of `{"item", "amount", "reason"}` for the "not_payable" items, with the clause in the reason.
- `unreadable`: items or amounts you could not read. Never guess a digit.
- `checks`: `{"items_add_up_to_total": true or false, "matches_hospital_form_total": true, false or "no hospital form"}`.
- `open_questions`: a list of things a person must confirm.

Do the arithmetic. `amount_claimed` plus `amount_not_claimed` must equal `amount_billed`. If it does not, or either check is false, set `confidence` below 0.88.
