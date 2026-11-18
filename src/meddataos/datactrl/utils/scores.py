"""Clinical scores computed from table rows.

Each calculator names the inputs it needs. An input is read from the table when
the table holds it, at the draw nearest the requested date, and is otherwise
taken from ``inputs`` when the caller supplies it. A score with a missing input
is returned incomplete with the list of what is missing. It is never estimated.

"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from meddataos.datactrl.errors import InvalidFetchOptions
from meddataos.datactrl.utils.table import (
    COLUMN_KEYS,
    Table,
    clean_number,
    column_name_key,
    column_unit,
    is_null,
    parse_number,
    parse_when,
    series,
)

DEFAULT_WINDOW_DAYS = 14


@dataclass(frozen=True)
class Needed:
    """One input of a score.

    ``key`` is the measurement the table is searched for. ``choices`` maps the
    words a caller may give for a graded finding to its grade.
    """

    name: str
    ask: str
    key: str | None = None
    unit: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    kind: str = "number"


@dataclass(frozen=True)
class Score:
    """A named calculator."""

    name: str
    title: str
    unit: str
    formula: str
    needs: tuple[Needed, ...]
    compute: Callable[[dict[str, Any]], dict[str, Any]]


_BILIRUBIN = Needed(
    "bilirubin", "total bilirubin in mg/dL", "lab.bilirubin_total", "mg/dL"
)
_ALBUMIN = Needed("albumin", "albumin in g/dL", "lab.albumin", "g/dL")
_INR = Needed("inr", "INR", "lab.inr")
_CREATININE = Needed("creatinine", "creatinine in mg/dL", "lab.creatinine", "mg/dL")

_ASCITES = (
    ("none", "none"),
    ("absent", "none"),
    ("no", "none"),
    ("mild", "mild"),
    ("slight", "mild"),
    ("moderate", "moderate to severe"),
    ("severe", "moderate to severe"),
    ("large", "moderate to severe"),
    ("tense", "moderate to severe"),
)
_ENCEPHALOPATHY = (
    ("none", "none"),
    ("absent", "none"),
    ("no", "none"),
    ("grade 1", "grade 1 to 2"),
    ("grade 2", "grade 1 to 2"),
    ("mild", "grade 1 to 2"),
    ("grade 3", "grade 3 to 4"),
    ("grade 4", "grade 3 to 4"),
    ("severe", "grade 3 to 4"),
)


def _band(value: float, low: float, high: float, rising: bool) -> int:
    """1, 2, or 3 points for a value below, within, or above a band."""
    if value < low:
        return 1 if rising else 3

    if value > high:
        return 3 if rising else 1

    return 2


def _child_pugh(given: dict[str, Any]) -> dict[str, Any]:
    grades = {"none": 1, "mild": 2, "grade 1 to 2": 2}
    points = {
        "bilirubin": _band(given["bilirubin"], 2.0, 3.0, rising=True),
        "albumin": _band(given["albumin"], 2.8, 3.5, rising=False),
        "inr": _band(given["inr"], 1.7, 2.3, rising=True),
        "ascites": grades.get(given["ascites"], 3),
        "encephalopathy": grades.get(given["encephalopathy"], 3),
    }
    total = sum(points.values())
    grade = "A" if total <= 6 else "B" if total <= 9 else "C"
    return {"value": total, "label": f"Class {grade}", "points": points}


def _albi(given: dict[str, Any]) -> dict[str, Any]:
    bilirubin_umol = given["bilirubin"] * 17.1
    albumin_g_l = given["albumin"] * 10
    value = round(math.log10(bilirubin_umol) * 0.66 - albumin_g_l * 0.085, 2)
    grade = 1 if value <= -2.60 else 2 if value <= -1.39 else 3
    return {"value": value, "label": f"Grade {grade}"}


def _meld(given: dict[str, Any]) -> dict[str, Any]:
    bilirubin = max(given["bilirubin"], 1.0)
    inr = max(given["inr"], 1.0)
    creatinine = min(max(given["creatinine"], 1.0), 4.0)
    raw = (
        3.78 * math.log(bilirubin)
        + 11.2 * math.log(inr)
        + 9.57 * math.log(creatinine)
        + 6.43
    )
    return {"value": min(round(raw), 40), "label": ""}


def _fib4(given: dict[str, Any]) -> dict[str, Any]:
    value = round(
        given["age"] * given["ast"] / (given["platelets"] * math.sqrt(given["alt"])),
        2,
    )
    label = (
        "below 1.45"
        if value < 1.45
        else "above 3.25"
        if value > 3.25
        else "1.45 to 3.25"
    )
    return {"value": value, "label": label}


def _bmi(given: dict[str, Any]) -> dict[str, Any]:
    metres = given["height"] / 100
    return {"value": round(given["weight"] / metres**2, 1), "label": ""}


def _interval(given: dict[str, Any]) -> dict[str, Any]:
    days = (given["to_date"] - given["from_date"]).days
    return {"value": days, "label": f"{round(days / 365.25, 2)} years"}


SCORES: dict[str, Score] = {
    score.name: score
    for score in (
        Score(
            "child_pugh",
            "Child-Pugh score",
            "points",
            "One to three points each for bilirubin (under 2, 2 to 3, over 3 mg/dL), "
            "albumin (over 3.5, 2.8 to 3.5, under 2.8 g/dL), INR (under 1.7, 1.7 to "
            "2.3, over 2.3), ascites (none, mild, moderate to severe), and "
            "encephalopathy (none, grade 1 to 2, grade 3 to 4). 5 to 6 points is "
            "class A, 7 to 9 class B, 10 to 15 class C.",
            (
                _BILIRUBIN,
                _ALBUMIN,
                _INR,
                Needed(
                    "ascites",
                    "ascites: none, mild, or moderate to severe",
                    choices=_ASCITES,
                    kind="grade",
                ),
                Needed(
                    "encephalopathy",
                    "hepatic encephalopathy: none, grade 1 to 2, or grade 3 to 4",
                    choices=_ENCEPHALOPATHY,
                    kind="grade",
                ),
            ),
            _child_pugh,
        ),
        Score(
            "albi",
            "ALBI score",
            "",
            "log10(bilirubin in umol/L) x 0.66 minus albumin in g/L x 0.085. "
            "Grade 1 is -2.60 or lower, grade 2 is above -2.60 up to -1.39, "
            "grade 3 is above -1.39.",
            (_BILIRUBIN, _ALBUMIN),
            _albi,
        ),
        Score(
            "meld",
            "MELD score",
            "points",
            "3.78 x ln(bilirubin mg/dL) + 11.2 x ln(INR) + 9.57 x ln(creatinine "
            "mg/dL) + 6.43, rounded. Values under 1 count as 1, creatinine is capped "
            "at 4, and the score at 40. This is the original MELD, without sodium "
            "or a dialysis adjustment.",
            (_BILIRUBIN, _INR, _CREATININE),
            _meld,
        ),
        Score(
            "fib4",
            "FIB-4 index",
            "",
            "age in years x AST divided by (platelets in 10^9/L x square root of ALT).",
            (
                Needed("age", "age in years", "demographics.age", "years"),
                Needed("ast", "AST in U/L", "lab.ast", "U/L"),
                Needed("alt", "ALT in U/L", "lab.alt", "U/L"),
                Needed("platelets", "platelets in 10^9/L", "lab.platelets", "10^9/L"),
            ),
            _fib4,
        ),
        Score(
            "bmi",
            "Body mass index",
            "kg/m2",
            "weight in kg divided by the square of height in metres.",
            (
                Needed("weight", "weight in kg", "vital.weight", "kg"),
                Needed("height", "height in cm", "vital.height", "cm"),
            ),
            _bmi,
        ),
        Score(
            "interval",
            "Interval between two dates",
            "days",
            "to_date minus from_date in days. Years are days divided by 365.25.",
            (
                Needed("from_date", "from_date as YYYY-MM-DD", kind="date"),
                Needed("to_date", "to_date as YYYY-MM-DD", kind="date"),
            ),
            _interval,
        ),
    )
}


def available() -> list[dict[str, Any]]:
    """Each score with the inputs it needs, for describe."""
    return [
        {
            "name": score.name,
            "title": score.title,
            "inputs": [n.name for n in score.needs],
        }
        for score in SCORES.values()
    ]


def compute(
    table: Table,
    name: object,
    *,
    at: object = None,
    inputs: object = None,
    window_days: object = None,
) -> dict[str, Any]:
    """Compute one score from the table and any supplied inputs.

    Args:
        table: The table to read measurements from.
        name: A key of ``SCORES``.
        at: Use the draw nearest this date. Without it, the latest draw.
        inputs: Values the table does not hold, by input name.
        window_days: How far from ``at`` a draw may be. Default 14.

    Returns:
        The score, each input with where it came from, and what is missing.

    Raises:
        InvalidFetchOptions: For an unknown score, date, or input value.
    """
    score = SCORES.get(str(name or "").strip().lower())

    if score is None:
        raise InvalidFetchOptions(
            f"Unknown score {name!r}; available: {', '.join(SCORES)}"
        )

    supplied = inputs if isinstance(inputs, dict) else {}
    when = None

    if at is not None and str(at).strip():
        when = parse_when(at)

        if when is None:
            raise InvalidFetchOptions("at must be a date such as 2011-03-10")

    window = int(parse_number(window_days) or DEFAULT_WINDOW_DAYS)
    given: dict[str, Any] = {}
    components: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []

    for need in score.needs:
        found = _supplied(need, supplied.get(need.name))

        if found is None:
            found = _from_table(table, need, when, window)

        if found is None:
            missing.append({"input": need.name, "ask": need.ask})
            continue

        given[need.name] = found.pop("_value")
        components.append({"input": need.name, **found})

    result: dict[str, Any] = {
        "name": score.name,
        "title": score.title,
        "formula": score.formula,
        "components": components,
        "complete": not missing,
    }

    if when is not None:
        result["at"] = when.date().isoformat()
        result["window_days"] = window

    if missing:
        result["missing"] = missing
        return result

    try:
        outcome = score.compute(given)
    except (ValueError, ZeroDivisionError) as exc:
        raise InvalidFetchOptions(f"{score.title} cannot be computed: {exc}") from exc

    for component in components:
        points = (outcome.get("points") or {}).get(component["input"])

        if points is not None:
            component["points"] = points

    result["value"] = clean_number(outcome["value"])
    result["unit"] = score.unit

    if outcome.get("label"):
        result["label"] = outcome["label"]

    return result


def _supplied(need: Needed, value: object) -> dict[str, Any] | None:
    """An input the caller gave, checked against what the score accepts."""
    if value is None or str(value).strip() == "":
        return None

    if need.kind == "grade":
        # Whole words only, so "unknown" is not read as "no".
        text = str(value).strip().lower()
        grade = next(
            (
                label
                for word, label in need.choices
                if re.search(rf"\b{re.escape(word)}\b", text)
            ),
            None,
        )

        if grade is None:
            raise InvalidFetchOptions(
                f"{need.name} must be {need.ask.split(': ', 1)[-1]}"
            )

        return {"value": grade, "source": "provided", "_value": grade}

    if need.kind == "date":
        when = parse_when(value)

        if when is None:
            raise InvalidFetchOptions(f"{need.name} must be a date such as 2011-03-10")

        day = when.date()
        return {"value": day.isoformat(), "source": "provided", "_value": day}

    number = parse_number(value)

    if number is None:
        raise InvalidFetchOptions(f"{need.name} must be a number ({need.ask})")

    return {
        "value": clean_number(number),
        "unit": need.unit,
        "source": "provided",
        "_value": number,
    }


def _from_table(
    table: Table, need: Needed, when: datetime | None, window: int
) -> dict[str, Any] | None:
    """The table's value for this input nearest ``when``, or None."""
    if need.key is None:
        return None

    if table.shape == "long":
        try:
            found = series(table, item=need.key)
        except InvalidFetchOptions:
            return None

        point = _nearest(found["points"], when, window)

        if point is None:
            return None

        return {
            "value": point["value"],
            "unit": found["unit"],
            "source": {
                "row": point["row"],
                "date": point["date"],
                "item": found["item"],
            },
            "_value": float(point["value"]),
        }

    name = next(
        (
            column
            for column in table.columns
            if COLUMN_KEYS.get(column_name_key(column)) == need.key
        ),
        None,
    )

    if name is None:
        return None

    time_column = table.roles.get("time") if table.shape == "wide" else None
    rows: list[dict[str, Any]] = []

    for index, row in enumerate(table.rows):
        number = parse_number(row.get(name))

        if number is None or is_null(row.get(name)):
            continue

        day = parse_when(row.get(time_column)) if time_column else None
        rows.append(
            {
                "row": index,
                "value": clean_number(number),
                "date": day.date().isoformat() if day else None,
            }
        )

    dated = sorted(
        (row for row in rows if row["date"]), key=lambda row: str(row["date"])
    )
    point = _nearest(dated, when, window) if dated else (rows[-1] if rows else None)

    if point is None:
        return None

    source = {"row": point["row"], "column": name}

    if point["date"]:
        source["date"] = point["date"]

    return {
        "value": point["value"],
        "unit": column_unit(name) or need.unit,
        "source": source,
        "_value": float(point["value"]),
    }


def _nearest(
    points: list[dict[str, Any]], when: datetime | None, window: int
) -> dict[str, Any] | None:
    """The latest point, or the one closest to ``when`` inside the window."""
    if not points:
        return None

    if when is None:
        return points[-1]

    target = when.date()
    limit = timedelta(days=window)

    def distance(point: dict[str, Any]) -> tuple[timedelta, bool]:
        day = datetime.fromisoformat(point["date"]).date()
        return abs(day - target), day > target

    best = min(points, key=distance)
    return best if distance(best)[0] <= limit else None
