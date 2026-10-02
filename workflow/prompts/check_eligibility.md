Your job in this step: say whether the policy is expected to cover this incident.

Read
- `task.context.policy.output.schedule`: the policy schedule (period, sum insured, waiting periods).
- `task.context.policy.output.documents`: the policy wording.
- `task.context.checks.output`: checks already made without a model. `policy_status` says whether the policy was in force on the admission date. Trust it; do not recompute it.
- `task.context.incident.output`: your earlier findings on the incident.

Return these fields
- `decision`: "covered", "not_covered" or "needs_review".
  - "covered" only when `policy_status` is "active", the wording covers this kind of event, and no waiting period or exclusion applies.
  - "not_covered" when `policy_status` is not "active", or a clause clearly excludes the event.
  - "needs_review" in every other case, including when `pre_existing_related` is "yes" or "unclear" and the waiting period may apply. Set `confidence` below 0.88 with it.
- `reasons`: short sentences, each tied to a clause or a check.
- `clauses`: a list of `{"clause": number or heading, "quote": exact words}` you relied on.
- `open_questions`: a list of things a person must confirm.
