Your job in this step: understand the incident and check it against the medical history.

Read
- `task.context.transcribe.output.transcript`: what the insured said about the incident. It is speech-to-text, so allow for small transcription errors, and list anything garbled in `open_questions`. If `task.context.transcribe.output.note` is present, the recording was cut short: say so in `open_questions`.
- `task.context.medical_records.output.documents`: their earlier medical reports, each with `file` and `text`.

Return these fields
- `incident_summary`: two to four plain sentences on what happened, when, and what treatment followed, taken from the transcript.
- `incident_date`: YYYY-MM-DD, or "" if the insured did not say.
- `cause`: "injury" or "illness".
- `injury_or_illness`: a short name for it.
- `pre_existing_related`: "no", "yes" or "unclear". Whether any condition recorded in the medical reports before the incident is related to it. Use "unclear" when the reports are missing or do not settle it.
- `pre_existing_evidence`: a list of `{"source": file name, "quote": exact words}` from the medical reports that support your answer.
- `open_questions`: a list of things a person must confirm.
