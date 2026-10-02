# Claim filing kit for AgenticOrg

Everything needed to run the eight-state health-insurance claim flow as an AgenticOrg workflow: two connector plugins, the workflow definition, synthetic demo documents, and scripts to test and register it.

**Status.** The whole workflow runs through AgenticOrg's real engine in the dry run and in the tests (Linux, Python 3.14; the previous version also on macOS). Live voice capture was run end to end in a real browser with a simulated microphone, against a stand-in for Gnani. Not yet run: a live AgenticOrg server, a real model, and the live Gnani service (recorded clips or realtime). Those need your server, your model key and your Gnani key.

## What is real and what is simulated

| Part | In this kit |
|---|---|
| Patient's form (Part A) | **Real.** Filled as a fillable PDF you can open, correct and sign. |
| Hospital's form (Part B) | **Simulated input.** A sample filled Part B sits in the demo vault. In real use the hospital fills it; the agent reads it and checks it is complete. If it is missing, the agent pre-fills the patient section of a blank Part B for the hospital and stops. |
| Reading the bill | **Real.** AgenticOrg's own extractor, with OCR for scans. |
| Vault of medical and policy documents | **Simulated.** A folder of synthetic documents. Point it at your own folder to use real ones. |
| Voice transcription | **Real**, through Gnani, two ways: spoken live into a capture page and transcribed as you speak (realtime), or a recorded clip of up to 60 seconds. The plain dry run and the tests use a fixed transcript instead. |
| Approval by the insured | **Real.** AgenticOrg's approval step. |
| Sending the claim to the insurer | **Simulated.** The workflow builds the claim package and a receipt marked simulated. Nothing is uploaded. |

Upload is simulated for two reasons. No insurer offers an upload API to build against here. And AgenticOrg does not let a workflow perform a filing at all today (see "What AgenticOrg will not let a workflow do").

## How the eight states map to steps

| State | Step | What runs |
|---|---|---|
| 1 Transcription | `transcribe` | `gnani_stt:query`, no model. Takes a statement captured live, or a recorded clip. |
| 2 Understanding the incident | `medical_records`, `incident` | Fetch reports from the vault, then the analyst agent |
| 3 Claim eligibility | `policy`, `checks`, `eligibility`, `eligibility_gate` | Fetch the policy, check dates without a model, the analyst agent, then a branch |
| 4 Coverage summary | `bill`, `hospital_form`, `coverage` | Read the bill (OCR for scans) and Part B, then the analyst agent |
| 5 Incident report | `report` | The analyst agent |
| 6 Filling the form | `fill_form`, `hospital_gate` | Fill Part A as a PDF, then branch on whether Part B is in |
| 7 User approval | `approval` | AgenticOrg's approval queue |
| 8 Submission | `package` | Build the claim package; the upload stays with a person |

Two branches end the run early: `stop_not_eligible` (policy not in force, or the incident is not covered) and `hospital_form_needed` (Part B missing).

The analyst agent has no tools. Every document is fetched by a workflow step, and the agent only reasons over what those steps return. The form and package tools are called by the workflow, never by the model.

## Try it

You need Python 3.12 or newer and a copy of AgenticOrg's source.

```bash
git clone --depth 1 https://github.com/mishrasanjeev/agentic-org.git ~/agentic-org   # if you do not have it
bash scripts/setup_and_test.sh ~/agentic-org
```

The script builds `.venv` in this folder, installs AgenticOrg's dependencies and both plugins, runs the tests, and runs the workflow once as a dry run. The filled form lands in `demo-output/DEMO-MEMBER-001/DEMO-CLAIM-001/claim_form_part_a.pdf`. The first run takes a few minutes.

More dry runs, from the AgenticOrg folder:

```bash
cd ~/agentic-org
KIT=/path/to/claim-filing-kit
$KIT/.venv/bin/python $KIT/scripts/dry_run.py --claim DEMO-CLAIM-002                            # Part B missing
$KIT/.venv/bin/python $KIT/scripts/dry_run.py --member DEMO-MEMBER-002 --claim DEMO-CLAIM-003   # policy lapsed
$KIT/.venv/bin/python $KIT/scripts/dry_run.py --claim DEMO-CLAIM-004                            # scanned bill
$KIT/.venv/bin/python $KIT/scripts/dry_run.py --reject                                          # form rejected
```

The scanned bill needs Tesseract and Poppler (`brew install tesseract poppler` on macOS). Without them the bill is reported as unreadable, never guessed.

In a dry run the four model replies and Gnani's reply are scripted stand-ins, because there is no model key or Gnani key. Everything else is real.

## Speak the statement live

This is state 1 with a microphone instead of a file. It needs your Gnani key.

```bash
export GNANI_API_KEY='your-key'
.venv/bin/python agenticorg-gnani/scripts/check_realtime.py     # is the key accepted for realtime?
.venv/bin/python -m agenticorg_gnani.voice_server               # opens http://localhost:8765
```

On the page: choose the language, press **Start recording**, speak, pause briefly between sentences, press **Stop and save**. Each sentence is written on the page when you pause. The page then shows a reference such as `vs_0123456789abcdef`. Run the workflow on what you said:

```bash
cd ~/agentic-org
$KIT/.venv/bin/python $KIT/scripts/dry_run.py --voice-session vs_0123456789abcdef
```

In a dry run, state 1 is then your own words, but states 2 to 5 are still scripted for the demo claim and do not react to them. They do on your AgenticOrg, with a model configured. The demo documents describe one incident, so for a demo that hangs together, say something like:

> On the fourteenth of September I slipped on the wet stairs at home and fell on my right hand. My wrist swelled up, so I went to Sample City Hospital. They said it was fractured, set it under anaesthesia and put on a cast. I was discharged the next day.

The capture page and its small server run on the computer with the microphone, and nowhere else: see `agenticorg-gnani/README.md` for how it works and what it guards.

## Put it in a running AgenticOrg

This needs an AgenticOrg you run yourself. The hosted app at app.agenticorg.ai cannot load plugins unless its operators install them.

1. Install both plugins into AgenticOrg's Python environment, for the API and every worker:
   ```bash
   python3 -m pip install ./agenticorg-gnani ./agenticorg-claimdesk
   ```
2. Set these for the API and every worker, then restart both:
   ```
   AGENTICORG_PLUGIN_LOADING=true
   AGENTICORG_PLUGIN_ALLOWLIST=agenticorg-gnani,agenticorg-claimdesk
   ```
   The startup log shows `plugin_loaded` for `gnani_stt` and `claim_desk`.
3. Copy `agenticorg-gnani/manifests/gnani_stt.json` and `agenticorg-claimdesk/manifests/claim_desk.json` into AgenticOrg's `manifests/` folder.
4. Register the connectors and the workflow:
   ```bash
   export AGENTICORG_URL=http://localhost:8000 AGENTICORG_TOKEN=... COMPANY_ID=... GNANI_API_KEY=...
   python3 scripts/register.py setup
   ```
5. Start a run, from a statement captured live or from a recorded clip:
   ```bash
   python3 scripts/register.py run WORKFLOW_ID --voice-session vs_0123456789abcdef
   python3 scripts/register.py run WORKFLOW_ID --audio-url https://.../voice-note.wav --language en-IN
   ```
   With `--voice-session`, run this on the computer where the statement was captured. The script reads the saved statement there and sends its text with the run, so AgenticOrg itself can be on another machine or in a container.
6. When the run waits, open the filled form from the outbox, sign it, and approve in AgenticOrg's Approvals screen.

Things AgenticOrg needs for this to work, all found in its source:

- **A model.** Agent steps fail unless AgenticOrg has a Gemini, Anthropic or OpenAI key, or a local model runtime, configured.
- **A company.** Workflow steps refuse to run without a `company_id`; `register.py` puts it in the run payload, and registers both connectors under the same company.
- **At least one shared agent in the workspace.** AgenticOrg attaches every approval item to an agent. With no shared agent, the approval never appears in the queue.
- **For a recorded clip, a public https link.** The Gnani connector downloads it; it cannot read a private Drive link or a local file. A statement captured live needs no link.

`register.py` has not been run against a live server. Its request bodies are tested against AgenticOrg's own request schemas.

## What AgenticOrg will not let a workflow do

AgenticOrg checks every connector tool call against a fixed list of action names in `core/governance/action_policy.py`.

- A tool whose name is not on the list is refused (`action_unknown`). A plugin cannot add names. So both connectors use names from the list: `query`, `read_record`, `list_records`, `check_status` (read) and `prepare_draft`, `build_filing_payload` (draft).
- Actions classed as writes, filings or payments are held in "shadow" and not carried out. A workflow connector step has no way to authorise one.

So a real upload to an insurer cannot be a workflow step in AgenticOrg as it stands. The workflow ends with a package that a person uploads.

## Using your own documents and forms

- **Vault.** Set `claim_desk_vault_dir` in the connector's config to a folder laid out as in `agenticorg-claimdesk/src/agenticorg_claimdesk/vault.py`: one folder per member with `profile.json`, `policy/`, `medical/` and `claims/<claim>/`.
- **Forms.** Set `claim_desk_forms_dir` to a folder holding `claim_form_part_a.pdf` and `claim_form_part_b.pdf`. They must be fillable PDFs. If their field names differ from this kit's, add a `field_map.json` there: `{"part_a": {"policy_number": "TheirFieldName"}}`. A flat, non-fillable PDF is not supported.
- **Prompts.** Edit the files in `workflow/prompts`, then run `python3 workflow/build_workflow.py`.

## Limits

- **Recorded clips: 60 seconds.** Gnani's limit for clips. Live capture is not bound by it: a recording runs up to 5 minutes by default, 14 at most.
- **Live capture works on one computer.** The page needs `localhost` for the microphone and holds no sign-in, so it is not something to put on the internet as it is.
- **Live text arrives per sentence.** Gnani sends each sentence when the speaker pauses, not word by word.
- **Low confidence pauses the run.** If the model reports confidence under 0.88, AgenticOrg stops for a human review at that step.
- **A rejected approval ends the run.** To change the form and try again, start a new run.
- **The signature is never filled.** The insured signs Part A; the hospital signs and seals Part B.
- **Coverage is an estimate.** The insurer decides the claim.
- **Real documents go to your model provider.** Medical reports and policy text are sent to whichever model AgenticOrg is configured with. Use the synthetic data until that is acceptable for real ones.

## What is in the kit

| Path | What |
|---|---|
| `agenticorg-gnani/` | Gnani speech-to-text, version 0.3.0: the connector, live voice capture, and checks of your key |
| `agenticorg-claimdesk/` | Claim-desk connector: vault, bill reading, PDF form filling, package |
| `workflow/` | The workflow definition, its builder and the agent prompts |
| `scripts/setup_and_test.sh` | Build the environment, run the tests and the dry run |
| `scripts/dry_run.py` | Run the workflow through AgenticOrg's engine with no server |
| `scripts/register.py` | Register the connectors and workflow in a running AgenticOrg |
| `tests/` | Workflow and registration tests |
| `CLAUDE.md` | Steps for Claude Code |

Version 0.1.0 of the Gnani connector named its tool `transcribe_audio`, which AgenticOrg refuses in every real run. Replace any installed copy with the one here.
