r"""Score one delivered answer against one eval question.

A question comes from ``evals/questions/<area>.json``:

    {
      "id": "tab-meld",
      "patient_id": "SYN-EVAL-01",
      "type": "score",
      "question": "What was the MELD score on 2020-07-22?",
      "expect": [["re:\\b17\\b"]],
      "expect_number": {"min": 1.9, "max": 2.7, "units": ["ml", "cm3"]},
      "expect_conflict": "tumor.size_cm",
      "expect_abstain": false
    }

``expect`` is a list of groups; each group is a list of alternatives, and every
group needs one alternative in the response. An alternative is a lowercase
substring, ``re:<regex>``, or ``date:YYYY-MM-DD`` (matched in the usual spellings).

The answer is what the server delivered: a JSON ``{"response", "citations"}``,
or plain text when nothing was cited.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

# Wording that says the files do not hold the answer. The final answer has no
# abstention field, so this is the only signal at the server's edge.
ABSTENTION_PHRASES = (
    "not documented",
    "not recorded",
    "not reported",
    "not available",
    "not found",
    "no record",
    "does not contain",
    "do not contain",
    "doesn't contain",
    "don't contain",
    "does not include",
    "do not include",
    "is not in the",
    "are not in the",
    "no mention",
    "not mentioned",
    "cannot be determined",
    "can't be determined",
    "unable to find",
    "could not find",
)

# Numbers written with thousands separators or a unicode minus still count.
NUMBER_PATTERN = re.compile(r"[-−]?\d[\d,]*(?:\.\d+)?")


@dataclass
class ParsedAnswer:
    """The response text and citations of one delivered answer."""

    response: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    is_json: bool = False


@dataclass
class Checks:
    """What each kind of expectation found. None means the question did not ask."""

    terms: bool | None = None
    missing_terms: list[list[str]] = field(default_factory=list)
    number: bool | None = None
    conflict: bool | None = None
    abstained: bool = False
    cited: bool = False


def load_questions(folder: Path) -> list[dict[str, Any]]:
    """Every question in ``folder``, tagged with its area (the file's stem)."""
    questions = []

    for path in sorted(folder.glob("*.json")):
        for item in json.loads(path.read_text()):
            questions.append({"area": path.stem, **item})

    return questions


def parse_answer(content: str) -> ParsedAnswer:
    """The response text and citations of a delivered answer."""
    text = content.strip()

    # A fenced reply is unwrapped the same way the server does for display.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ParsedAnswer(response=text)

    if not isinstance(parsed, dict):
        return ParsedAnswer(response=text)

    response = str(parsed.get("response") or parsed.get("message") or "")
    citations = [
        item for item in parsed.get("citations") or [] if isinstance(item, dict)
    ]
    return ParsedAnswer(response=response, citations=citations, is_json=True)


def date_spellings(iso_date: str) -> list[str]:
    """Common ways an answer writes one date, lowercase."""
    day = date.fromisoformat(iso_date)
    month = MONTH_NAMES[day.month - 1]
    short_month = month[:3]

    return [
        day.isoformat(),
        day.strftime("%Y/%m/%d"),
        f"{month} {day.day}, {day.year}",
        f"{month} {day.day} {day.year}",
        f"{short_month} {day.day}, {day.year}",
        f"{short_month} {day.day} {day.year}",
        f"{day.day} {month} {day.year}",
        f"{day.day} {short_month} {day.year}",
    ]


def term_matches(expected: str, text: str) -> bool:
    """One alternative against lowercase response text."""
    if expected.startswith("re:"):
        return re.search(expected[3:], text) is not None

    if expected.startswith("date:"):
        return any(spelling in text for spelling in date_spellings(expected[5:]))

    return expected.lower() in text


def number_matches(spec: dict[str, Any], text: str) -> bool:
    """Whether a number in range is written next to one of the expected units."""
    units = [unit.lower() for unit in spec.get("units") or []]

    for match in NUMBER_PATTERN.finditer(text):
        number = float(match.group().replace(",", "").replace("−", "-"))
        if not spec["min"] <= number <= spec["max"]:
            continue

        # A unit must follow within a few characters, so "2.1 mL" counts but
        # a "2" from a date or a series number does not.
        following = text[match.end() : match.end() + 6].strip()
        if not units or any(following.startswith(unit) for unit in units):
            return True

    return False


def looks_like_abstention(response: str) -> bool:
    """Whether the response says the files do not hold the answer."""
    lowered = response.lower()
    return any(phrase in lowered for phrase in ABSTENTION_PHRASES)


def check(item: dict[str, Any], answer: ParsedAnswer) -> Checks:
    """Run every expectation the question carries."""
    text = answer.response.lower()
    checks = Checks(
        abstained=looks_like_abstention(answer.response), cited=bool(answer.citations)
    )

    groups = item.get("expect") or []
    if groups:
        checks.missing_terms = [
            alternatives
            for alternatives in groups
            if not any(term_matches(alternative, text) for alternative in alternatives)
        ]
        checks.terms = not checks.missing_terms

    if item.get("expect_number"):
        checks.number = number_matches(item["expect_number"], text)

    if item.get("expect_conflict"):
        checks.conflict = item["expect_conflict"] in conflict_attributes(answer)

    return checks


def conflict_attributes(answer: ParsedAnswer) -> set[str]:
    """Attributes the answer's note citations flag as disagreeing."""
    attributes = set()

    for citation in answer.citations:
        for conflict in citation.get("conflicts") or []:
            if isinstance(conflict, dict) and conflict.get("attribute"):
                attributes.add(str(conflict["attribute"]))

    return attributes


def verdict(item: dict[str, Any], checks: Checks) -> bool:
    """Whether the answer is correct for this question.

    Grounding is not part of the verdict: ``checks.cited`` is counted on its own in
    the summary, so a regression shows as wrong answers or as uncited ones, never
    as one blended number.
    """
    # An abstention may still cite the files it searched; only the wording counts.
    if item.get("expect_abstain"):
        return checks.abstained

    # Every expectation the question carries must hold. None means it was not asked,
    # and a question with no expectation at all is refused by the question tests.
    asked = [checks.terms, checks.number, checks.conflict]
    return all(result for result in asked if result is not None)
