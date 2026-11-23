"""How a citation names a case file: page paths and note-span links.

A delivered citation carries ``web_path`` (``/case/<quoted parts>``) for the page
to open the file, and a note quote carries ``span_url`` to highlight it. The
verifier writes them and the server serves them, so both read them from here.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote, urlparse


def web_path(relative: str) -> str:
    """URL path of a case file, with each part quoted."""
    parts = [quote(part) for part in Path(relative).parts if part not in ("", ".")]
    return "/case/" + "/".join(parts)


def note_span_url(patient_id: str, doc_id: str, start: int, end: int) -> str:
    """Page URL for one highlighted note span."""
    return (
        f"/api/cases/{quote(patient_id, safe='')}/notes/span"
        f"?id={quote(doc_id, safe='')}&start={int(start)}&end={int(end)}"
    )


def path_key(path: object) -> str:
    """Identity of a case path.

    Spaces and %20, and a missing /case prefix, are the same file.
    """
    text = str(path or "").strip()

    for _ in range(3):
        decoded = unquote(text)

        if decoded == text:
            break

        text = decoded

    if "://" in text:
        text = urlparse(text).path or text

    text = text.split("?", 1)[0].replace("\\", "/").rstrip("/")

    if text.startswith("/case/"):
        text = text[len("/case/") :]
    elif text.startswith("case/"):
        text = text[len("case/") :]

    return text
