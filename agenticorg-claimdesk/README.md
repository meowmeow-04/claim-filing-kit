# agenticorg-claimdesk

The non-voice tools of the claim workflow, as an AgenticOrg connector plugin named `claim_desk`. It reads the claimant's documents from a vault folder, fills the insured's claim form as a PDF, checks the hospital's form, and builds the claim package. It makes no network calls.

The kit's `README.md`, one folder up, explains how it is used. This page is the tool reference.

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `list_records` | read | Lists the files the vault holds for a member and a claim. |
| `read_record` | read | Returns the text of one kind of document: `medical_records`, `policy`, `bill` or `hospital_form`. Scans are read with OCR. |
| `check_status` | read | Checks, without a model, whether the policy was in force, and whether the bill and the hospital's form are in. |
| `prepare_draft` | draft | Fills Part A as a PDF in the outbox. With `form=part_b_request`, pre-fills the patient section of Part B for the hospital. |
| `build_filing_payload` | draft | After an approval, copies Part A, Part B and the bill into a package folder with a manifest. Uploads nothing. |

The names come from AgenticOrg's action taxonomy; a tool with any other name is refused at run time.

## Safeguards

- Member and claim references are checked before they touch a path, and a file outside the vault (a symlink, say) is ignored.
- The signature fields are never written.
- `build_filing_payload` refuses unless it is handed an approval whose decision is `approve`. `defer` and `reject` do not count.
- A file that cannot be read comes back as `readable: false` with the reason. Text is never guessed.
- The package records a hash of every file, and notes when Part A was changed after the agent drafted it.

## Config

All optional. Set them in the connector's `auth_config` when registering it. AgenticOrg needs at least one key there, so set the outbox.

| Key | Default |
|---|---|
| `claim_desk_outbox_dir` | `~/claim-desk-outbox` |
| `claim_desk_vault_dir` | the bundled synthetic demo vault |
| `claim_desk_forms_dir` | the bundled demo templates |

## Demo data

`src/agenticorg_claimdesk/demo` holds two fillable form templates and a vault with two synthetic members and four claims:

| Member | Claim | Shows |
|---|---|---|
| `DEMO-MEMBER-001` | `DEMO-CLAIM-001` | A complete claim |
| `DEMO-MEMBER-001` | `DEMO-CLAIM-002` | The hospital's form is missing |
| `DEMO-MEMBER-002` | `DEMO-CLAIM-003` | The policy ended before the admission |
| `DEMO-MEMBER-001` | `DEMO-CLAIM-004` | The bill is a scan, read with OCR |

The templates are generic demo layouts, not any insurer's form. Rebuild everything with `tools/build_demo_assets.py` (needs `reportlab`).

## Tests

`../scripts/setup_and_test.sh` runs them. No network is needed.
