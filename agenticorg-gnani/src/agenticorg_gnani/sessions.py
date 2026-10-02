"""Voice sessions: the saved result of one live capture.

The capture server writes one JSON file per session; the connector's ``query``
tool reads it back for workflow state 1. Both must see the same folder
(default ``~/gnani-voice-sessions``), so for now they run on the same machine.

A session holds what the person said about a health incident. The folder is
created private to the user (0700) and each file is written 0600.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_DIR = "~/gnani-voice-sessions"
SOURCE = "gnani_realtime"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def sessions_dir(value: Any = None) -> Path:
    return Path(str(value or DEFAULT_DIR)).expanduser()


def new_session_id() -> str:
    return f"vs_{secrets.token_hex(8)}"


def safe_session_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise ValueError("voice_session_id must be 1-64 letters, digits, hyphens or underscores")
    return text


def session_path(directory: Path, session_id: str) -> Path:
    return directory / f"{safe_session_id(session_id)}.json"


def save_session(directory: Path, session: dict[str, Any]) -> Path:
    """Write the session atomically and privately. Returns the file's path."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = session_path(directory, session["session_id"])
    handle, temp_name = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")  # created 0600
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(session, out, ensure_ascii=False, indent=2)
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return target


def load_session(directory: Path, session_id: str) -> dict[str, Any]:
    """Read a saved session. LookupError when there is none; ValueError when the id or file is not valid."""
    path = session_path(directory, session_id)
    root = directory.resolve()
    if not path.is_file() or path.resolve().parent != root:
        raise LookupError(f"no voice session {session_id!r} in {directory}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"voice session {session_id!r} could not be read") from exc
    if not isinstance(data, dict) or data.get("source") != SOURCE:
        raise ValueError(f"{path.name} is not a voice session written by the capture server")
    return data
