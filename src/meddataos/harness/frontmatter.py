"""The front matter shared by skills and agents.

Both are ``key: value`` lines between ``---`` fences, then a Markdown body. A
value may continue on indented lines, so a long ``requires`` or ``tools`` list
is not cut off at the first line. ``#`` lines are comments. Without a closing
fence the whole file is the body.
"""

from __future__ import annotations


def front_matter(text: str) -> tuple[dict[str, str], str]:
    """``key: value`` lines between ``---`` fences, and the body after them."""
    if not text.startswith("---\n"):
        return {}, text.strip()

    header, separator, rest = text[4:].partition("\n---")

    if not separator:
        return {}, text.strip()

    fields: dict[str, str] = {}
    current: str | None = None

    for line in header.splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        if current and (line[:1].isspace() or ":" not in line):
            fields[current] = f"{fields[current]} {stripped}"
            continue

        key, colon, value = line.partition(":")

        if colon and key.strip() and " " not in key.strip():
            current = key.strip().lower()
            fields[current] = value.strip()
            continue

        if current:
            fields[current] = f"{fields[current]} {stripped}"

    body = rest.lstrip("-").lstrip("\n").strip()
    return fields, body
