#!/usr/bin/env bash
# One command: build a Python environment with AgenticOrg's dependencies and
# both plugins, run every test, run the whole claim workflow once as a dry
# run, then (when GNANI_API_KEY is set) check the Gnani connector live, for
# recorded clips and for realtime.
#
#   bash scripts/setup_and_test.sh [agentic-org folder] [clip.wav] [language]
#
# Defaults: agentic-org folder = ~/agentic-org, language = en-IN.
# Set CLAIM_KIT_PYTHON=/path/to/python3.x to choose the Python (3.12 or newer).
# The environment is created in .venv inside this kit; your system Python and
# the AgenticOrg checkout are not changed. Safe to run again.
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd)"
AO_DIR="${1:-$HOME/agentic-org}"
CLIP="${2:-}"
LANGUAGE="${3:-en-IN}"
VENV="${CLAIM_KIT_VENV:-$KIT/.venv}"
OUTPUT="${CLAIM_KIT_OUTPUT:-$KIT/demo-output}"

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# macOS ships "python3", not "python"; "pip" only exists inside an environment.
find_python() {
  for candidate in ${CLAIM_KIT_PYTHON:+"$CLAIM_KIT_PYTHON"} python3.14 python3.13 python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

[ -f "$AO_DIR/connectors/framework/base_connector.py" ] || die "No AgenticOrg checkout at $AO_DIR.
Pass its folder as the first argument, or get one with:
  git clone --depth 1 https://github.com/mishrasanjeev/agentic-org.git \"$AO_DIR\""
AO_DIR="$(cd "$AO_DIR" && pwd)"
if [ -n "$CLIP" ]; then
  [ -f "$CLIP" ] || die "Audio file not found: $CLIP"
  CLIP="$(cd "$(dirname "$CLIP")" && pwd)/$(basename "$CLIP")"
fi

if [ ! -x "$VENV/bin/python" ]; then
  PYTHON="$(find_python)" || die "Python 3.12 or newer is required and was not found (looked for python3.14, python3.13, python3.12, python3)."
  say "Creating environment in $VENV with $("$PYTHON" --version)"
  existed=0
  [ -e "$VENV" ] && existed=1
  "$PYTHON" -m venv "$VENV" || {
    [ "$existed" = 0 ] && rm -rf "$VENV" # do not leave a half-built environment behind
    die "$PYTHON could not create a Python environment (see the message above)."
  }
fi
PY="$VENV/bin/python"
"$PY" -m pip --version >/dev/null 2>&1 || die "The environment in $VENV is broken (no pip). Delete that folder and run this again."

STAMP="$VENV/.agenticorg-deps-installed"
if [ ! -f "$STAMP" ] || [ "$AO_DIR/requirements.txt" -nt "$STAMP" ]; then
  say "Installing AgenticOrg's dependencies (a few minutes the first time)"
  "$PY" -m pip install --quiet --upgrade pip
  # pytesseract and pdf2image let AgenticOrg's extractor read scanned bills;
  # they only work when Tesseract and Poppler are installed on this machine.
  "$PY" -m pip install --quiet -r "$AO_DIR/requirements.txt" pytest pytest-asyncio pytesseract pdf2image
  touch "$STAMP"
fi

say "Installing the two plugins"
"$PY" -m pip install --quiet -e "$KIT/agenticorg-gnani" -e "$KIT/agenticorg-claimdesk"

cd "$AO_DIR" # both plugins import AgenticOrg's code from the checkout

run_tests() {
  AGENTICORG_ENV="test" "$PY" -m pytest "$1/tests" --rootdir "$1" \
    -p no:cacheprovider -o addopts="" -o asyncio_mode=auto -q -W ignore::DeprecationWarning
}
say "Tests: Gnani connector"
run_tests "$KIT/agenticorg-gnani"
say "Tests: claim-desk connector"
run_tests "$KIT/agenticorg-claimdesk"
say "Tests: the workflow, through AgenticOrg's engine"
run_tests "$KIT"

say "Dry run of the whole workflow (model and Gnani replies are scripted stand-ins)"
"$PY" "$KIT/scripts/dry_run.py" --outbox "$OUTPUT"
echo "Open the filled form: $OUTPUT/DEMO-MEMBER-001/DEMO-CLAIM-001/claim_form_part_a.pdf"

if command -v tesseract >/dev/null 2>&1 && command -v pdftoppm >/dev/null 2>&1; then
  echo "OCR for scanned bills: available (try: dry_run.py --claim DEMO-CLAIM-004)"
else
  echo "OCR for scanned bills: not installed. For scans, install Tesseract and Poppler (macOS: brew install tesseract poppler)."
fi

if [ -z "${GNANI_API_KEY:-}" ]; then
  say "Skipping the live Gnani check: GNANI_API_KEY is not set"
  exit 0
fi
say "Checking the Gnani connector against Gnani (recorded clips)"
failed=0
if [ -n "$CLIP" ]; then
  "$PY" "$KIT/agenticorg-gnani/scripts/smoke_test.py" "$CLIP" "$LANGUAGE" || failed=1
else
  "$PY" "$KIT/agenticorg-gnani/scripts/smoke_test.py" || failed=1
fi

say "Checking the key against Gnani's realtime endpoint (live voice capture)"
# It streams a 16-bit WAV clip; with any other clip it checks the key alone.
if [ -n "$CLIP" ]; then
  "$PY" "$KIT/agenticorg-gnani/scripts/check_realtime.py" "$CLIP" "$LANGUAGE" || failed=1
else
  "$PY" "$KIT/agenticorg-gnani/scripts/check_realtime.py" || failed=1
fi
[ "$failed" = 0 ] || die "A live Gnani check failed; its output is above."

cat <<EOF

To capture a statement from the microphone:
  $PY -m agenticorg_gnani.voice_server
then open http://localhost:8765, record, and run the workflow on what you said:
  (cd "$AO_DIR" && $PY "$KIT/scripts/dry_run.py" --voice-session <reference shown on the page>)
EOF
