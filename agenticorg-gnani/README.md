# agenticorg-gnani

Gnani (Vachana) speech-to-text for AgenticOrg, version 0.3.0. Two ways in, one API key:

| | Recorded clip | Live microphone |
|---|---|---|
| Gnani endpoint | `POST https://api.vachana.ai/stt/v3` | `wss://api.vachana.ai/stt/v3/stream` |
| Length | 60 seconds or less | up to 14 minutes (default limit 5) |
| How it reaches AgenticOrg | the connector downloads the clip and sends it to Gnani | spoken into the capture page, transcribed as you speak, saved as a voice session |
| Text appears | when the whole clip is done | each time the speaker pauses |

Both use the same key, sent in the `x-api-key-id` header. That is how Gnani's documentation and its own SDK (`gnani-vachana` 0.7.9) do it. Whether realtime is switched on for a particular account is something only Gnani's server can say: `scripts/check_realtime.py` asks it.

**Status.** Every part is tested against stand-ins for Gnani that follow its documented protocol, and the capture page was run in a real Chromium with a simulated microphone. Nothing here has yet been run against the live Gnani service: this was built where `api.vachana.ai` cannot be reached.

## Check your key

Run these with the kit's Python (`.venv/bin/python`, built by the kit's `scripts/setup_and_test.sh`). The key is read from the environment and never printed.

```bash
export GNANI_API_KEY='your-key'
python3 scripts/check_key.py                        # recorded clips; needs nothing installed
../.venv/bin/python scripts/check_realtime.py       # realtime
../.venv/bin/python scripts/check_realtime.py voice.wav hi-IN   # realtime, with a transcript of your clip
```

`check_realtime.py` prints three lines:

1. `[PASS] your key is accepted for realtime` means the key works on the realtime endpoint. A `[FAIL]` here carries Gnani's own reason; send that line to Gnani.
2. `[PASS] audio frames accepted`, or the transcript of your clip. The clip must be a 16-bit WAV.
3. `[PASS] a wrong key is refused`. If Gnani accepted a wrong key, line 1 would prove nothing, so this is checked.

## Live voice capture

```bash
export GNANI_API_KEY='your-key'
../.venv/bin/python -m agenticorg_gnani.voice_server
```

It opens `http://localhost:8765`. Choose the language, press **Start recording**, speak, and pause briefly between sentences: each sentence is written on the page when you pause. Press **Stop and save**. The page then shows a reference such as `vs_0123456789abcdef`. That reference is what starts a claim run:

```bash
python3 ../scripts/register.py run WORKFLOW_ID --voice-session vs_0123456789abcdef     # on your AgenticOrg
../.venv/bin/python ../scripts/dry_run.py --voice-session vs_0123456789abcdef          # dry run, from the AgenticOrg folder
```

Use Chrome first. Safari and Firefox should work but were not tested.

How it fits together:

```
microphone -> capture page -> voice server (this machine) -> Gnani realtime
                           <- text, sentence by sentence  <-
                              voice server saves ~/gnani-voice-sessions/vs_....json
```

The page cannot talk to Gnani directly. A browser cannot set the header the key travels in, and the key must not be given to a web page. So the small server in the middle holds the key, cuts the audio into the 1024-byte frames Gnani expects, keeps them at speaking speed, and saves the result.

Options: `--port 8765`, `--max-seconds 300` (5 to 840), `--sessions-dir DIR` (or `GNANI_SESSIONS_DIR`), `--save-audio` (also keep the recording as a WAV), `--no-open`.

What it guards:

- **This machine only.** It listens on 127.0.0.1 and refuses to start on any other address. A browser allows the microphone on `localhost` without https. To let other people use the page it would need an https address and a sign-in in front of it; it has neither.
- **Only its own page.** The audio socket accepts connections from the capture page alone, so another website open in the same browser cannot use your key through it.
- **The key stays in the server.** It is not in the page, in the saved files or in any message.
- **Private files.** A voice session is someone's account of a medical incident. The folder is readable by your user only, and the audio is not kept unless you ask with `--save-audio`.
- **Nothing is lost quietly.** If the tab closes, Gnani drops the connection, or the time limit is reached, what was transcribed so far is saved and marked `"complete": false` with the reason.

## The connector

Plugin entry point `gnani_stt`, one tool: `query`.

| Parameter | Meaning |
|---|---|
| `voice_session_id` | Reference of a statement captured live. With `voice_transcript` (the text, which `register.py` sends along) nothing else is needed. Without it, the session file is read from the capture folder, so AgenticOrg must be able to see that folder. |
| `audio_url` | Public https link to a recorded clip, 60 seconds or less. |
| `audio_base64` | A clip's bytes as base64. A few seconds at most. |
| `audio_format` | `wav`, `mp3`, `flac`, `ogg`, `m4a` or `aac`. Detected when omitted. |
| `language_code` | `bn-IN`, `en-IN`, `gu-IN`, `hi-IN`, `kn-IN`, `ml-IN`, `mr-IN`, `pa-IN`, `ta-IN` or `te-IN`. Default `en-IN`. A voice session read from the folder keeps the language chosen on the page. |
| `itn`, `native_numerals` | Clips only: write numbers, currency and dates as digits. |

Give exactly one of `voice_session_id`, `audio_url`, `audio_base64`. It returns `transcript`, `language_code` and `source` (`gnani_realtime` or `gnani_rest`). For a voice session no call to Gnani is made: it was transcribed while it was spoken.

Connector config:

| Key | Meaning |
|---|---|
| `gnani_api_key` | The API key. Preferred. |
| `api_key` | The API key, as a fallback. |
| `gnani_base_url` | Another API address, any public https host. |
| `base_url` | Another API address, honoured only on Gnani's own domains. |
| `gnani_sessions_dir` | Where voice sessions are read from. Default `~/gnani-voice-sessions`. |

The `gnani_` names exist because AgenticOrg hands an agent one config merged from all its connectors. A plain `api_key` or `base_url` there may belong to another connector, so the key is read from its own name first and is never sent to a host that is not Gnani's.

**Why the tool is called `query`.** AgenticOrg refuses any connector tool whose name is not in its action list (`core/governance/action_policy.py`). `query` is on the list as a read. A name such as `transcribe_audio` is refused in every real run.

The connector's test in AgenticOrg (`health_check`) transcribes half a second of silence with your key: `healthy` only if Gnani accepts the key.

## Install into AgenticOrg

```bash
python3 -m pip install ./agenticorg-gnani        # into AgenticOrg's environment, API and every worker
```

Set `AGENTICORG_PLUGIN_LOADING=true` and add `agenticorg-gnani` to `AGENTICORG_PLUGIN_ALLOWLIST`, copy `manifests/gnani_stt.json` into AgenticOrg's `manifests/` folder, and restart.

## Limits

- **Recorded clips: 60 seconds.** Gnani's limit for the clip endpoint. It does not apply to live capture.
- **Live capture: 15 minutes per session**, Gnani's limit. The server stops at `--max-seconds`, 5 minutes unless you change it.
- **Text arrives per sentence, not per word.** Gnani sends a sentence once it hears about half a second of silence. There are no partial words while someone is still talking.
- **Numbers as digits (`itn`) is for recorded clips.** Live capture gives the words as spoken.
- **One machine.** The capture page, its server and the saved sessions are on the computer with the microphone.

## Files

| Path | What |
|---|---|
| `src/agenticorg_gnani/connector.py` | The AgenticOrg connector |
| `src/agenticorg_gnani/realtime.py` | Client for Gnani's realtime endpoint |
| `src/agenticorg_gnani/voice_server.py`, `static/voice.html` | Live capture: the local server and its page |
| `src/agenticorg_gnani/sessions.py` | Saving and reading voice sessions |
| `scripts/check_key.py`, `scripts/check_realtime.py`, `scripts/smoke_test.py` | Live checks of your key |
| `tests/` | Tests; `fake_gnani_realtime.py` is the stand-in for Gnani's realtime server |
