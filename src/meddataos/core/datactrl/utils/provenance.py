"""Whether a number or date in an answer is one a table read returned.

One rule for every agent: a number taken from a table has to be a value the
fetch returned (a cell, a series statistic, a score), allowing for rounding. The
server applies it to every sentence cited only to table reads.
"""

from __future__ import annotations

import re
from typing import Any

from meddataos.core.datactrl.utils.table import parse_number

DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
# Digits that are part of a unit, such as 10^3/uL or mL/min/1.73m2.
UNIT_NUMBERS = re.compile(r"10\^\d+(?:/\w+)?|/?\s?1\.73\s?m2?")
# Not after a letter or a dot, so "c1", "B31f" and the ".1" of "tabular.1" are
# not numbers.
NUMBER = re.compile(r"(?<![\w.])[-+−]?\d+(?:,\d{3})*(?:\.\d+)?")

MONTH_NUMBERS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))
# "22 July 2020", "22 Jul" and "July 22, 2020", "Jul. 22", "July 2020". A month
# with neither a day nor a year ("may", "march") is a word, not a date.
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH})\b\.?,?(?:\s+(\d{{4}})\b)?", re.I)
MONTH_DAY = re.compile(
    rf"\b({_MONTH})\b\.?(?:\s+(\d{{1,2}})(?:st|nd|rd|th)?\b)?(?:,?\s+(\d{{4}})\b)?",
    re.I,
)
YEAR = re.compile(r"^(?:19|20)\d{2}$")

# Keys whose contents are not values: where a value sits (row numbers, the
# search window), and text that describes a score. A formula's thresholds and an
# input's "grade 1 to 2" would otherwise count as returned numbers.
NOT_VALUE_KEYS = frozenset(
    {"row", "row_numbers", "window_days", "formula", "title", "ask"}
)

# Rounding may not move a value by more than this share of it, so 212 stands for
# 211.6 but 3 does not stand for 2.78.
MAX_ROUNDING_SHARE = 0.05


def strip_non_values(text: str) -> str:
    """Text without dates, times and unit digits, whose numbers are not values."""
    return UNIT_NUMBERS.sub(" ", TIME.sub(" ", DATE.sub(" ", text)))


def collect_values(node: Any, numbers: list[float], strings: list[str]) -> None:
    """Every number and string a read returned, numbers inside labels included."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in NOT_VALUE_KEYS:
                collect_values(value, numbers, strings)
        return

    if isinstance(node, list):
        for item in node:
            collect_values(item, numbers, strings)
        return

    number = parse_number(node)

    if number is not None:
        numbers.append(number)
        return

    if isinstance(node, str):
        strings.append(node)
        # Numbers inside a label such as "Grade 1" count. The parts of a date do not.
        numbers.extend(
            float(found.replace(",", "").replace("−", "-"))
            for found in NUMBER.findall(strip_non_values(node))
        )


def decimals_of(text: str) -> int:
    """How many decimals a written number shows; scientific notation counts none."""
    if "." not in text or "e" in text.lower():
        return 0

    return min(len(text.split(".", 1)[1]), 9)


def rounds_to(given: float, held: float, decimals: int) -> bool:
    """Whether ``given`` is ``held`` rounded to that many decimals."""
    gap = abs(given - held)

    if gap <= 1e-9:
        return True

    return gap <= 0.5 * 10**-decimals + 1e-9 and gap <= MAX_ROUNDING_SHARE * abs(held)


def returned(value: object, data: Any) -> bool:
    """Whether a read returned this value, allowing for rounding.

    A string has to be one the read returned, ignoring case.
    """
    numbers: list[float] = []
    strings: list[str] = []
    collect_values(data, numbers, strings)
    number = parse_number(value)

    if number is None:
        wanted = str(value).strip().casefold()
        return wanted in {text.strip().casefold() for text in strings}

    decimals = decimals_of(str(value).strip())
    return any(rounds_to(number, found, decimals) for found in numbers)


def written_dates(sentence: str) -> list[tuple[str, str, tuple[int, int]]]:
    """Dates written in words: (as written, the ISO part it pins down, its span).

    The ISO part is what a returned date must contain: "July 22, 2020" pins
    "2020-07-22", "July 2020" pins "2020-07-", and "22 July" pins "-07-22".
    """
    found: list[tuple[str, str, tuple[int, int]]] = []
    taken: list[tuple[int, int]] = []

    def add(
        match: re.Match[str], month_name: str, day: str | None, year: str | None
    ) -> None:
        span = match.span()
        overlaps = any(span[0] < end and start < span[1] for start, end in taken)

        if overlaps or not (day or year):
            return

        month = MONTH_NUMBERS[month_name.lower()]
        month_day = f"-{month:02d}-{int(day):02d}" if day else f"-{month:02d}-"
        found.append((match.group(0).strip(), f"{year or ''}{month_day}", span))
        taken.append(span)

    for match in DAY_MONTH.finditer(sentence):
        add(match, match.group(2), match.group(1), match.group(3))

    for match in MONTH_DAY.finditer(sentence):
        add(match, match.group(1), match.group(2), match.group(3))

    return found


def unsupported(sentence: str, data: Any) -> list[str]:
    """Numbers and dates in a sentence that the read did not return.

    ``data`` should hold what was asked as well as what came back, so a date the
    agent asked about counts. A number may be rounded from the returned one, and
    its sign may be put in words ("fell by 3.4" for a change of -3.4). Dates may
    be written in words, and a bare year counts when a returned date is in it.
    """
    numbers: list[float] = []
    strings: list[str] = []
    collect_values(data, numbers, strings)
    haystack = " ".join(strings)
    missing = [day for day in DATE.findall(sentence) if day not in haystack]

    # Written dates are judged as dates, then blanked so their digits are not
    # judged again as values.
    remaining = sentence
    for written, iso_part, (start, end) in written_dates(sentence):
        if iso_part not in haystack:
            missing.append(written)
        remaining = remaining[:start] + " " * (end - start) + remaining[end:]

    for found in NUMBER.findall(strip_non_values(remaining)):
        text = found.replace(",", "").replace("−", "-")
        value = float(text)
        decimals = decimals_of(text)

        if any(rounds_to(abs(value), abs(held), decimals) for held in numbers):
            continue

        if YEAR.match(text) and f"{text}-" in haystack:
            continue

        missing.append(found)

    return missing
