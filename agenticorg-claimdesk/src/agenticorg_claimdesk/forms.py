"""Fill and read fillable PDF forms. Needs only pypdf, which AgenticOrg ships."""

from __future__ import annotations

import io
from typing import Any

from pypdf import PdfReader, PdfWriter


def read_fields(pdf: bytes) -> dict[str, str]:
    """Return ``{field name: value}`` for a fillable PDF; ``{}`` when it has no form fields."""
    try:
        fields = PdfReader(io.BytesIO(pdf)).get_fields() or {}
    except Exception:  # noqa: BLE001 - any unreadable PDF simply has no readable fields
        return {}
    values: dict[str, str] = {}
    for name, field in fields.items():
        value = field.get("/V")
        values[str(name)] = "" if value is None else str(value).strip()
    return values


def fill_form(
    template: bytes, values: dict[str, Any], field_map: dict[str, str] | None = None
) -> tuple[bytes, list[str]]:
    """Write ``values`` into the template's form fields.

    ``field_map`` translates this package's field names to the names a
    different fillable PDF uses. Returns ``(pdf bytes, names not found in the
    PDF)``. Fields stay editable so a person can correct them before signing.
    """
    field_map = field_map or {}
    reader = PdfReader(io.BytesIO(template))
    present = set((reader.get_fields() or {}).keys())
    translated: dict[str, str] = {}
    missing: list[str] = []
    for name, value in values.items():
        target = field_map.get(name, name)
        if target in present:
            translated[target] = "" if value is None else str(value)
        else:
            missing.append(name)

    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        writer.update_page_form_field_values(page, translated, auto_regenerate=False)
    writer.set_need_appearances_writer(True)  # viewers redraw the values in their own font
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), sorted(missing)
