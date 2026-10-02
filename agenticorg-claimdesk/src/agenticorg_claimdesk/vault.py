"""Read-only access to the document vault.

Layout, under the vault root::

    members/<member_id>/profile.json
    members/<member_id>/policy/policy.json          the policy schedule
    members/<member_id>/policy/<wording>.pdf
    members/<member_id>/medical/<reports>
    members/<member_id>/claims/<claim_ref>/<...bill...>
    members/<member_id>/claims/<claim_ref>/<...part_b...>    the hospital's form

Member and claim references come from a workflow payload, so they are checked
before they touch a path, and every file is confirmed to sit inside the root.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

RECORDS = ("medical_records", "policy", "bill", "hospital_form")
CLAIM_RECORDS = frozenset({"bill", "hospital_form"})
READABLE_SUFFIXES = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".txt", ".md", ".docx", ".html"}
)
MAX_FILES = 10
MAX_FILE_BYTES = 15 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def safe_id(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"{what} must be 1-64 letters, digits, hyphens or underscores")
    return text


class Vault:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def member_ids(self) -> list[str]:
        members = self.root / "members"
        return sorted(p.name for p in members.iterdir() if p.is_dir()) if members.is_dir() else []

    def member_dir(self, member_id: str) -> Path:
        path = self._inside(self.root / "members" / safe_id(member_id, "member_id"))
        if not path.is_dir():
            raise LookupError(f"No member {member_id!r} in the vault")
        return path

    def claim_dir(self, member_id: str, claim_ref: str) -> Path:
        return self._inside(self.member_dir(member_id) / "claims" / safe_id(claim_ref, "claim_ref"))

    def profile(self, member_id: str) -> dict[str, Any]:
        return self._json(self.member_dir(member_id) / "profile.json")

    def policy_schedule(self, member_id: str) -> dict[str, Any]:
        return self._json(self.member_dir(member_id) / "policy" / "policy.json")

    def files(self, member_id: str, record: str, claim_ref: str = "") -> list[Path]:
        if record not in RECORDS:
            raise ValueError(f"record must be one of {', '.join(RECORDS)}")
        if record in CLAIM_RECORDS:
            if not claim_ref:
                raise ValueError(f"claim_ref is required to read the {record}")
            folder = self.claim_dir(member_id, claim_ref)
            if record == "bill":
                wanted = lambda stem: "bill" in stem  # noqa: E731
            else:
                wanted = lambda stem: "part_b" in stem or "hospital_form" in stem  # noqa: E731
        else:
            folder = self.member_dir(member_id) / ("medical" if record == "medical_records" else "policy")
            wanted = lambda stem: True  # noqa: E731
        if not folder.is_dir():
            return []
        found = [
            path
            for path in sorted(folder.iterdir())
            if path.is_file()
            and path.suffix.lower() in READABLE_SUFFIXES
            and wanted(path.stem.lower())
            and path.stat().st_size <= MAX_FILE_BYTES
            and self._is_inside(path)
        ]
        return found[:MAX_FILES]

    # ----------------------------------------------------------------- helpers

    def _is_inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            return False
        return True

    def _inside(self, path: Path) -> Path:
        if not self._is_inside(path):
            raise ValueError("path is outside the vault")
        return path

    def _json(self, path: Path) -> dict[str, Any]:
        if not path.is_file() or not self._is_inside(path):
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
