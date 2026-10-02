# Claim filing kit for AgenticOrg

Two AgenticOrg connector plugins (`agenticorg-gnani`, `agenticorg-claimdesk`), a workflow definition (`workflow/health_claim_filing.json`) and scripts. `README.md` explains the design; read it before changing anything.

Status: the tests and a dry run pass on Linux with Python 3.14, through AgenticOrg's real engine at commit `94c62a3`; an earlier version passed on macOS. Live voice capture was run in a real Chromium with a simulated microphone against a stand-in for Gnani. Not yet run: a live AgenticOrg server, a real model, the live Gnani API (recorded clips or realtime), a real microphone, Safari, Firefox.

## Rules

- Secrets (`GNANI_API_KEY`, `AGENTICORG_TOKEN`) stay in environment variables. Never print them, write them to a file, or put them on a command line. To check one is set: `[ -n "$GNANI_API_KEY" ] && echo set`.
- Use `python3`, not `python` or `pip`. Outside an activated environment macOS has neither.
- Do not edit files in the AgenticOrg checkout. The kit works without changing it.
- Do not rename a connector tool. AgenticOrg refuses any tool name that is not in its action taxonomy (`core/governance/action_policy.py`); the current names were chosen from it. Do not add a tool that uploads, submits or files anything: AgenticOrg will not run it, and the design keeps the upload with a person.
- Never turn off HTTPS certificate verification.
- The demo data is synthetic and says so on every page. Keep it that way.

## "Set this up and test it"

Do these in order. Stop at the first failure, show the exact output, then fix it.

1. **AgenticOrg source.** Ask whether the user has a checkout. If not:
   ```bash
   git clone --depth 1 https://github.com/mishrasanjeev/agentic-org.git ~/agentic-org
   ```
2. **Build, test and dry-run.** Allow 10 minutes; the first run installs about 100 packages:
   ```bash
   bash scripts/setup_and_test.sh ~/agentic-org
   ```
   Expect three test runs, each ending in `N passed` with nothing failed, then a trace of all eight states ending `Run status: completed`. Tests that need OCR are skipped when Tesseract or Poppler is missing; that is fine. When `GNANI_API_KEY` is set the script goes on to the live Gnani checks in step 4.
3. **Show the result.** Open `demo-output/DEMO-MEMBER-001/DEMO-CLAIM-001/claim_form_part_a.pdf` for the user (`open <path>` on macOS).
4. **Gnani, live.** Only if `GNANI_API_KEY` is set. A speech clip (60 seconds or less) is optional; a 16-bit WAV also gets a realtime transcript.
   ```bash
   python3 agenticorg-gnani/scripts/check_key.py ["<clip>" <language>]
   .venv/bin/python agenticorg-gnani/scripts/check_realtime.py ["<clip.wav>" <language>]
   ```
   Expect three `PASS` lines from each. The first line of `check_realtime.py` is the answer to "does this key have the realtime API". If it fails, show the user that line exactly: it carries Gnani's reason, and it is Gnani who must switch realtime on.
5. **Live voice capture.** Only after `check_realtime.py` passes. See the next section.
6. **Report** what passed, what was skipped and why. Stop here unless the user asks to put it on a server.

## "Capture my voice live"

1. Start the capture server and leave it running. It opens the page in the browser; use Chrome first.
   ```bash
   .venv/bin/python -m agenticorg_gnani.voice_server
   ```
2. The user chooses the language, presses **Start recording**, allows the microphone, speaks, pauses between sentences, and presses **Stop and save**. Text appears at each pause. The page shows a reference like `vs_0123456789abcdef`, and the server prints the same reference in its terminal.
3. Run the workflow on it, in a second terminal opened in this kit's folder:
   ```bash
   KIT="$PWD"
   (cd ~/agentic-org && "$KIT/.venv/bin/python" "$KIT/scripts/dry_run.py" --voice-session <reference>)
   ```
   State 1 must show the user's words with `captured live, Gnani realtime`. States 2 to 5 of a dry run are scripted for the demo claim; say so.
4. Stop the server with Ctrl+C when done.

Do not change these: the server listens on 127.0.0.1 only, accepts its audio socket from its own page only, and keeps the key out of the page and the saved files. Do not expose it through a tunnel or bind it to another address; the page has no sign-in.

If live capture fails:

- **The page says Gnani did not accept the connection.** Run `check_realtime.py` and show its first line.
- **`Address already in use`.** Add `--port 8766`.
- **The microphone is blocked.** macOS: System Settings, Privacy & Security, Microphone, allow the browser; then allow it for `localhost` in the address bar.
- **No text appears, or it is garbled.** Check the language chosen on the page. Rerun with `--save-audio` and play the WAV saved next to the session in `~/gnani-voice-sessions`: if the recording itself sounds wrong, switch browser (Chrome is the tested one); if it sounds right, the problem is on Gnani's side and `check_realtime.py "<that wav>" <language>` reproduces it.
- **`Saved, but the recording ended early`.** The session file's `problems` list says why.
- **Gnani's realtime protocol differs from what the code expects** (for example, no `connected` message, or a different refusal). The client is `agenticorg-gnani/src/agenticorg_gnani/realtime.py`, written from Gnani's docs and SDK; `tests/fake_gnani_realtime.py` encodes the same assumptions. Change both together and show the user what Gnani actually sent.

## If a step fails

- **pip cannot install a package.** Delete `.venv` and rerun with another Python 3.12 or newer: `CLAIM_KIT_PYTHON=/path/to/python3.13 bash scripts/setup_and_test.sh ...`.
- **Tests fail in `action policy` tests.** AgenticOrg's taxonomy changed. Read `core/governance/action_policy.py` in the checkout and pick tool names from its read and draft groups; change the name in the connector's `_register_tools`, its `tools` list, its manifest and `workflow/build_workflow.py`, then rebuild the workflow.
- **Workflow tests fail after an AgenticOrg update.** The engine's behaviour is described at the top of `workflow/build_workflow.py`; check those three statements against `workflows/step_types.py` and `workflows/engine.py` in the checkout.
- **`check_realtime.py` cannot connect, with a certificate error.** The kit's `.venv` has the `certifi` certificates and the client uses them; make sure it is run with `.venv/bin/python`, not the system `python3`.
- **`check_key.py`: the key line is not 2xx, or the wrong-key line is not 401/403.** The Gnani connector's health check assumes those answers. The logic is `health_check` in `agenticorg-gnani/src/agenticorg_gnani/connector.py`; the cases are in `test_health_check_outcomes`. Change both to match what Gnani returns. A wrong key must never report `healthy`.

## "Put it on my AgenticOrg"

Only when the user asks. First find out where their AgenticOrg runs.

- **The hosted app at app.agenticorg.ai:** they cannot install plugins there. Say so and stop.
- **They run it themselves:** follow "Put it in a running AgenticOrg" in `README.md`, in order. To start AgenticOrg locally, follow the "Local development" section of AgenticOrg's own README; do not improvise the steps. Then:
  - Get a tenant admin's access token (AgenticOrg's login endpoint returns `access_token`) and the company's id; export them as `AGENTICORG_TOKEN` and `COMPANY_ID`.
  - Run `python3 scripts/register.py setup`. Both connector tests must print `healthy`. `register.py` has not been run against a live server; if a call fails it prints the server's reply, which says what to change.
  - Check the requirements listed in the README: a model, a company, one shared agent, and for a recorded clip a public https link.
  - Run `python3 scripts/register.py run <workflow id> --voice-session <reference>` for a statement captured live (run it on the machine where it was captured), or `--audio-url <link> --language <code>` for a recorded clip.
  - Use the synthetic demo member until the user has decided that real medical documents may be sent to their model provider.
