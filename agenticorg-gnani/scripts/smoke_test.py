"""Live check of the Gnani connector against the real API.

Run inside AgenticOrg's Python environment, from the AgenticOrg checkout:

    GNANI_API_KEY=... python /path/to/agenticorg-gnani/scripts/smoke_test.py clip.wav hi-IN

Checks, in order:
  1. health with your key           -> must be "healthy"
  2. health with a wrong key        -> must be "unhealthy"
  3. transcription of the clip      -> prints the transcript (skipped without a file)

Set GNANI_BASE_URL only if Gnani gave you an endpoint other than https://api.vachana.ai.
Exit code 0 means every check passed. The key is never printed.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.getcwd())  # the AgenticOrg checkout, when it is not pip-installed
try:
    from agenticorg_gnani.connector import GnaniSttConnector
except ModuleNotFoundError as exc:
    sys.exit(
        f"Cannot import {exc.name!r}. Run this from the AgenticOrg checkout, in AgenticOrg's Python "
        "environment, after `pip install /path/to/agenticorg-gnani`."
    )


async def _health(config: dict[str, Any]) -> dict[str, Any]:
    connector = GnaniSttConnector(config)
    await connector.connect()
    try:
        return await connector.health_check()
    finally:
        await connector.disconnect()


async def main() -> int:
    key = os.environ.get("GNANI_API_KEY", "").strip()
    if not key:
        print(__doc__)
        return 2
    config: dict[str, Any] = {"gnani_api_key": key}
    if os.environ.get("GNANI_BASE_URL", "").strip():
        config["gnani_base_url"] = os.environ["GNANI_BASE_URL"].strip()
    failures = 0

    good = await _health(config)
    passed = good.get("status") == "healthy"
    failures += not passed
    print(f"[{'PASS' if passed else 'FAIL'}] health with your key   -> {good}")

    bad = await _health({**config, "gnani_api_key": "invalid-" + "0" * 24})
    passed = bad.get("status") == "unhealthy"
    failures += not passed
    print(f"[{'PASS' if passed else 'FAIL'}] health with a wrong key -> {bad}")

    if len(sys.argv) > 1:
        clip = Path(sys.argv[1]).expanduser()
        language = sys.argv[2] if len(sys.argv) > 2 else "en-IN"
        connector = GnaniSttConnector(config)
        await connector.connect()
        try:
            result = await connector.execute_tool(
                "query",
                {"audio_base64": base64.b64encode(clip.read_bytes()).decode(), "language_code": language},
            )
            print(f"[PASS] transcribe {clip.name} ({language}) -> {result}")
        except Exception as exc:  # noqa: BLE001 - a smoke test reports whatever went wrong
            failures += 1
            body = getattr(getattr(exc, "response", None), "text", "")
            print(f"[FAIL] transcribe {clip.name} -> {type(exc).__name__}: {exc} {body[:300]}")
        finally:
            await connector.disconnect()
    else:
        print("[SKIP] transcription: pass an audio file (60 s or less) as the first argument")

    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
