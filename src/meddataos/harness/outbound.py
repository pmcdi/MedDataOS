"""Screen what the harness sends off the machine for this patient's details.

Reference pages are fetched from public sites, so a URL is an outbound message:
anything the model puts in it reaches that site's logs. The prompt says "never
patient details"; this is the check behind it.

A URL is refused when it holds one of the case's identifiers (the case folder
name, patient ids found in the files, the patient's name once the card has it)
or a full calendar date, which on a reference site is almost always a birth,
scan or visit date. Matching is on normalised words, so ``RADCURE-0006``,
``radcure_0006`` and ``radcure%200006`` all match.

What it does not catch: a surname alone, free-text clinical detail, or a value
the files never name. It narrows the channel; the allowlist and the dropped
query strings narrow it further.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from urllib.parse import unquote_plus

# Identifiers shorter than this are too likely to be ordinary words.
MIN_TERM_CHARS = 4
FULL_DATE = re.compile(
    r"(?<!\d)(?:"
    r"(?:19|20)\d{2}[-/._ ](?:0?[1-9]|1[0-2])[-/._ ](?:0?[1-9]|[12]\d|3[01])"
    r"|(?:0?[1-9]|[12]\d|3[01])[-/._ ](?:0?[1-9]|1[0-2])[-/._ ](?:19|20)\d{2}"
    r"|(?:0?[1-9]|1[0-2])[-/._ ](?:0?[1-9]|[12]\d|3[01])[-/._ ](?:19|20)\d{2}"
    r")(?!\d)"
)


def words(text: str) -> str:
    """Lowercase letters and digits, every other run of characters one space."""
    return " ".join(re.findall(r"[a-z0-9]+", unquote_plus(text).lower()))


class OutboundScreen:
    """Refuses outbound text that names this patient."""

    def __init__(self, identifiers: Callable[[], Iterable[str]] | None = None) -> None:
        """``identifiers`` is asked on every check: the card fills in later."""
        self.identifiers = identifiers or (lambda: ())

    def problem(self, text: str) -> str | None:
        """Why ``text`` must not leave the machine, or None.

        The reason never repeats the identifier it found.
        """
        decoded = unquote_plus(text)

        if FULL_DATE.search(decoded):
            return "it contains a full date, which may be the patient's"

        # Padded with spaces so a term matches whole words only.
        haystack = f" {words(decoded)} "
        squashed = haystack.replace(" ", "")

        for identifier in self.identifiers():
            term = words(str(identifier))
            joined = term.replace(" ", "")

            if len(joined) < MIN_TERM_CHARS:
                continue

            # An id with digits also matches with its separators gone
            # (RADCURE0006); a name does not, or "smith" would hit "goldsmith".
            if f" {term} " in haystack or (
                re.search(r"\d", joined) and joined in squashed
            ):
                return "it contains this patient's identifier or name"

        return None
