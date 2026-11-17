"""Typed reading of small clinical tables.

A table file is a list of rows of strings. This module gives those rows types
(number, date, category, text), finds which columns play which role (time, item,
value, unit, reference range), and answers the questions a chart review asks of a
table: filtered rows, and one measurement over time with its first, last, lowest
and highest value. Nothing here calls a model, and every returned value names the
file row it came from.

"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from meddataos.datactrl.errors import InvalidFetchOptions

# Placeholders that mean "no value". "none" is not one of them: in a registry
# column such as Chemo or vascular invasion it is the recorded answer.
NULL_TOKENS = frozenset(
    {
        "",
        "na",
        "n/a",
        "nan",
        "[not available]",
        "[not applicable]",
        "[not evaluated]",
        "[unknown]",
    }
)

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_THOUSANDS = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_SLASH_DATES = ("%m/%d/%Y", "%Y/%m/%d")


def is_null(value: object) -> bool:
    """Whether a cell holds no value."""
    return value is None or str(value).strip().lower() in NULL_TOKENS


def parse_number(value: object) -> float | None:
    """The cell as a number, or None. A censored result such as ``<5`` is not one."""
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if _THOUSANDS.fullmatch(text):
        text = text.replace(",", "")

    return float(text) if _NUMBER.fullmatch(text) else None


def parse_when(value: object) -> datetime | None:
    """The cell as a date or date-time, or None."""
    if isinstance(value, datetime):
        return value

    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    text = str(value or "").strip()

    if len(text) < 8 or not text[0].isdigit():
        return None

    try:
        # Offsets are dropped so every time in a table compares with the others.
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass

    for pattern in _SLASH_DATES:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue

    return None


def clean_number(value: float) -> float | int:
    """A float without binary noise, and a whole number as an int."""
    rounded = round(value, 6)
    return int(rounded) if rounded == int(rounded) else rounded


# ---------------------------------------------------------------------------
# Known measurements: one key, the names a lab uses for it, and unit factors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Analyte:
    """A measurement the harness can recognise and bring to one unit.

    ``factors`` maps a reported unit to the multiplier that gives ``unit``.
    Only conversions that are plain arithmetic are listed. A unit that is not
    listed is reported as it is and kept out of the statistics.

    """

    key: str
    label: str
    names: tuple[str, ...]
    loinc: tuple[str, ...]
    unit: str
    factors: dict[str, float] = field(default_factory=dict)


def _unit_key(unit: object) -> str:
    text = str(unit or "").strip().lower()
    text = text.replace("µ", "u").replace("μ", "u").replace(" ", "")
    text = text.replace("x10", "10").replace("10e", "10^").replace("**", "^")
    return {
        "k/ul": "10^3/ul",
        "10^3/mm3": "10^3/ul",
        "10^3/mm^3": "10^3/ul",
        "thou/ul": "10^3/ul",
        "s": "sec",
        "secs": "sec",
        "seconds": "sec",
        "iu/l": "u/l",
        "meq/l": "mmol/l",
        "uiu/ml": "miu/l",
    }.get(text, text)


def _analyte(
    key: str,
    label: str,
    names: str,
    loinc: str,
    unit: str,
    **factors: float,
) -> Analyte:
    table = {_unit_key(unit): 1.0}
    table.update({_unit_key(name): factor for name, factor in factors.items()})
    return Analyte(
        key=key,
        label=label,
        names=tuple(part.strip() for part in names.split(",")),
        loinc=tuple(part.strip() for part in loinc.split(",") if part.strip()),
        unit=unit,
        factors=table,
    )


_SAME_COUNT = {"10^9/L": 1.0, "10^3/uL": 1.0}

ANALYTES: tuple[Analyte, ...] = (
    _analyte(
        "lab.afp",
        "AFP",
        "afp, alpha fetoprotein, alpha 1 fetoprotein",
        "1834-1",
        "ng/mL",
        **{"ug/L": 1.0},
    ),
    _analyte(
        "lab.bilirubin_total",
        "Bilirubin, total",
        "bilirubin total, total bilirubin, bilirubin, t bili, tbili",
        "1975-2",
        "mg/dL",
        **{"umol/L": 1 / 17.1},
    ),
    _analyte(
        "lab.albumin", "Albumin", "albumin, alb", "1751-7", "g/dL", **{"g/L": 0.1}
    ),
    _analyte(
        "lab.creatinine",
        "Creatinine",
        "creatinine, creat, cr",
        "2160-0",
        "mg/dL",
        **{"umol/L": 1 / 88.4},
    ),
    _analyte(
        "lab.platelets",
        "Platelets",
        "platelets, platelet count, platelet, plt",
        "777-3",
        "10^3/uL",
        **_SAME_COUNT,
    ),
    _analyte(
        "lab.hemoglobin",
        "Hemoglobin",
        "hemoglobin, haemoglobin, hgb, hb",
        "718-7",
        "g/dL",
        **{"g/L": 0.1},
    ),
    _analyte(
        "lab.wbc",
        "WBC",
        "wbc, white blood cells, white cell count, leukocytes",
        "6690-2",
        "10^3/uL",
        **_SAME_COUNT,
    ),
    _analyte(
        "lab.ast", "AST", "ast, sgot, aspartate aminotransferase", "1920-8", "U/L"
    ),
    _analyte("lab.alt", "ALT", "alt, sgpt, alanine aminotransferase", "1742-6", "U/L"),
    _analyte("lab.alp", "ALP", "alp, alkaline phosphatase, alk phos", "6768-6", "U/L"),
    _analyte("lab.pt", "PT", "pt, prothrombin time", "5902-2", "sec"),
    _analyte("lab.inr", "INR", "inr, international normalized ratio", "6301-6", ""),
    _analyte("lab.sodium", "Sodium", "sodium, na", "2951-2", "mmol/L"),
    _analyte("lab.potassium", "Potassium", "potassium, k", "2823-3", "mmol/L"),
    _analyte(
        "lab.calcium",
        "Calcium",
        "calcium, ca, calcium total",
        "17861-6",
        "mg/dL",
        **{"mmol/L": 4.008},
    ),
    _analyte("lab.tsh", "TSH", "tsh, thyroid stimulating hormone", "3016-3", "mIU/L"),
    _analyte("lab.egfr", "eGFR", "egfr, estimated gfr", "33914-3", "mL/min/1.73m2"),
    _analyte(
        "lab.hcv_rna", "HCV RNA", "hcv rna, hcv viral load", "38180-6", "log10 IU/mL"
    ),
    _analyte("lab.hcv_genotype", "HCV genotype", "hcv genotype", "32286-7", ""),
)

_BY_NAME = {name: item for item in ANALYTES for name in item.names}
_BY_LOINC = {code: item for item in ANALYTES for code in item.loinc}
_BY_KEY = {item.key: item for item in ANALYTES}


def _name_key(text: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def find_analyte(name: object = None, code: object = None) -> Analyte | None:
    """The known measurement for a test name, a key such as ``lab.afp``, or a LOINC."""
    by_code = _BY_LOINC.get(str(code or "").strip())

    if by_code:
        return by_code

    text = str(name or "").strip()
    return (
        _BY_KEY.get(text.lower())
        or _BY_LOINC.get(text)
        or _BY_NAME.get(_name_key(text))
    )


# Wide-table columns: what they measure, and the unit their name implies.
_COLUMN_UNITS = (
    ("_mg_dl", "mg/dL"),
    ("_g_dl", "g/dL"),
    ("_mmhg", "mmHg"),
    ("_per_min", "/min"),
    ("_bpm", "bpm"),
    ("_pct", "%"),
    ("_kg", "kg"),
    ("_cm", "cm"),
    ("_gy", "Gy"),
    ("_cgy", "cGy"),
    ("_c", "°C"),
)

# Only names that mean one thing in any table. "dose" and "status" are left out:
# a medication list and a radiotherapy record both have them.
COLUMN_KEYS = {
    "weight_kg": "vital.weight",
    "weight": "vital.weight",
    "height_cm": "vital.height",
    "height": "vital.height",
    "bmi": "vital.bmi",
    "temp_c": "vital.temperature",
    "hr_bpm": "vital.heart_rate",
    "sbp_mmhg": "vital.sbp",
    "dbp_mmhg": "vital.dbp",
    "spo2_pct": "vital.spo2",
    "ecog": "performance.ecog",
    "ecog_ps": "performance.ecog",
    "ecog_performance_status": "performance.ecog",
    "age": "demographics.age",
    "age_at_initial_pathologic_diagnosis": "demographics.age",
    "sex": "demographics.sex",
    "gender": "demographics.sex",
    "smoking_status": "social_history.smoking",
    "smoking_py": "social_history.pack_years",
    "cumulative_dose_gy": "rt.cumulative_dose_gy",
    "fx": "rt.fractions",
    "fractions_delivered": "rt.fractions_delivered",
    "ajcc_pathologic_stage": "tumor.stage",
    "neoplasm_histologic_grade": "tumor.grade",
    "vital_status": "outcome.vital_status",
}


def column_name_key(name: object) -> str:
    """A column name in lowercase with runs of punctuation as one underscore."""
    return "_".join(re.findall(r"[a-z0-9]+", str(name or "").lower()))


def column_unit(name: str) -> str:
    """The unit a wide-table column name ends in, such as kg for ``weight_kg``."""
    key = column_name_key(name)

    for suffix, unit in _COLUMN_UNITS:
        if key.endswith(suffix):
            return unit

    return ""


# ---------------------------------------------------------------------------
# Profile: column types, roles, and the shape of the table
# ---------------------------------------------------------------------------

_ROLE_NAMES: dict[str, tuple[str, ...]] = {
    "item": (
        "test",
        "test_name",
        "analyte",
        "item",
        "parameter",
        "component",
        "observation",
        "measurement",
        "lab_test",
    ),
    "value": ("value", "result", "result_value", "numeric_value"),
    "unit": ("unit", "units", "uom"),
    "ref_low": ("ref_low", "reference_low", "lower_limit", "normal_low"),
    "ref_high": ("ref_high", "reference_high", "upper_limit", "normal_high"),
    "flag": ("flag", "abnormal_flag", "abnormal"),
    "status": ("status", "result_status"),
    "code": ("loinc", "loinc_code", "code"),
}

_TIME_NAMES = (
    "collected_at",
    "collection_date",
    "drawn_at",
    "result_date",
    "observation_date",
    "visit_date",
    "date",
    "datetime",
    "timestamp",
    "time",
)

MAX_CATEGORY_VALUES = 12
MIXED_NUMERIC_SHARE = 0.8


def _column_profile(values: list[Any]) -> dict[str, Any]:
    present = [value for value in values if not is_null(value)]
    profile: dict[str, Any] = {"nulls": len(values) - len(present)}

    if not present:
        return {"type": "empty", **profile}

    numbers = [parse_number(value) for value in present]
    numeric = [number for number in numbers if number is not None]

    if len(numeric) == len(present):
        return {
            "type": "number",
            **profile,
            "min": clean_number(min(numeric)),
            "max": clean_number(max(numeric)),
        }

    times = [parse_when(value) for value in present]

    if all(times):
        found = sorted(item for item in times if item)
        return {
            "type": "date",
            **profile,
            "min": found[0].date().isoformat(),
            "max": found[-1].date().isoformat(),
        }

    distinct = list(dict.fromkeys(str(value).strip() for value in present))
    profile["distinct"] = len(distinct)

    if len(numeric) >= MIXED_NUMERIC_SHARE * len(present):
        return {"type": "mixed", **profile}

    if len(distinct) <= MAX_CATEGORY_VALUES:
        return {"type": "category", **profile, "values": distinct}

    return {"type": "text", **profile, "examples": distinct[:3]}


@dataclass
class Table:
    """Rows of one table file with their column types and roles."""

    columns: list[str]
    rows: list[dict[str, Any]]
    types: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    shape: str = "table"

    @classmethod
    def build(cls, columns: list[str], rows: list[dict[str, Any]]) -> Table:
        """Type the columns, then find the roles and the shape."""
        table = cls(columns=list(columns), rows=rows)
        table.types = {
            name: _column_profile([row.get(name) for row in rows]) for name in columns
        }
        table.roles = table._find_roles()
        table.shape = table._find_shape()
        return table

    def _find_roles(self) -> dict[str, str]:
        by_key = {column_name_key(name): name for name in self.columns}
        roles: dict[str, str] = {}

        for role, names in _ROLE_NAMES.items():
            found = next((by_key[name] for name in names if name in by_key), None)

            if found:
                roles[role] = found

        dated = [name for name in self.columns if self.types[name]["type"] == "date"]
        start = next((n for n in dated if "start" in column_name_key(n)), None)
        stop = next(
            (
                n
                for n in dated
                if any(
                    part in column_name_key(n).split("_") for part in ("stop", "end")
                )
            ),
            None,
        )

        if start:
            roles["start"] = start

        if stop:
            roles["stop"] = stop

        timed = [name for name in dated if name not in (start, stop)]
        named = next(
            (by_key[key] for key in _TIME_NAMES if by_key.get(key) in timed), None
        )

        if named or timed:
            roles["time"] = named or timed[0]

        # A value column is only a measurement when a column says what was measured.
        if "item" not in roles:
            for role in ("value", "unit", "ref_low", "ref_high", "flag", "code"):
                roles.pop(role, None)

            # Without results to supersede, a status column is ordinary data.
            roles.pop("status", None)

        return roles

    def _find_shape(self) -> str:
        roles = self.roles

        if "item" in roles and "value" in roles and "time" in roles:
            return "long"

        if "start" in roles and "stop" in roles:
            return "intervals"

        if len(self.rows) == 1:
            return "record"

        if "time" in roles:
            measured = [
                name
                for name in self.columns
                if self.types[name]["type"] in ("number", "mixed")
            ]
            return "wide" if measured else "events"

        return "table"

    # -----------------------------------------------------------------------
    # What can be asked for
    # -----------------------------------------------------------------------

    def items(self, limit: int) -> list[dict[str, Any]]:
        """Each measurement in a long table, with its count, dates, and unit."""
        if self.shape != "long":
            return []

        found: dict[str, dict[str, Any]] = {}

        for row in self.rows:
            name = str(row.get(self.roles["item"]) or "").strip()

            if not name:
                continue

            entry = found.setdefault(name, {"item": name, "n": 0})
            entry["n"] += 1
            when = parse_when(row.get(self.roles["time"]))

            if when:
                day = when.date().isoformat()
                entry["first"] = min(entry.get("first", day), day)
                entry["last"] = max(entry.get("last", day), day)

            known = find_analyte(name, row.get(self.roles.get("code", "")))

            if known:
                entry["key"] = known.key
                entry["unit"] = known.unit
            elif "unit" in self.roles and "unit" not in entry:
                entry["unit"] = str(row.get(self.roles["unit"]) or "").strip()

        return list(found.values())[:limit]

    def measured_columns(self) -> list[str]:
        """Numeric columns of a wide table that can be read as a series."""
        if self.shape != "wide":
            return []

        return [
            name
            for name in self.columns
            if self.types[name]["type"] in ("number", "mixed")
        ]

    def column(self, name: object) -> str | None:
        """The column with this name, matched without case or punctuation."""
        wanted = column_name_key(name)
        return next(
            (item for item in self.columns if column_name_key(item) == wanted), None
        )


# ---------------------------------------------------------------------------
# Filtered rows
# ---------------------------------------------------------------------------

_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "contains", "in")


def _compare(cell: object, op: str, wanted: object) -> bool:
    if op == "in":
        options = wanted if isinstance(wanted, list) else [wanted]
        return any(_compare(cell, "eq", option) for option in options)

    if op == "contains":
        return str(wanted).strip().lower() in str(cell or "").lower()

    left_number, right_number = parse_number(cell), parse_number(wanted)
    left: Any
    right: Any

    if left_number is not None and right_number is not None:
        left, right = left_number, right_number
    else:
        left_time, right_time = parse_when(cell), parse_when(wanted)

        if left_time and right_time:
            # A date with no time of day covers the whole day.
            if len(str(wanted).strip()) <= 10:
                left_time = datetime(left_time.year, left_time.month, left_time.day)

            left, right = left_time, right_time
        elif op in ("eq", "ne"):
            left = str(cell or "").strip().lower()
            right = str(wanted or "").strip().lower()
        else:
            return False

    outcomes = {
        "eq": left == right,
        "ne": left != right,
        "gt": left > right,
        "ge": left >= right,
        "lt": left < right,
        "le": left <= right,
    }
    return bool(outcomes[op])


def filter_rows(
    table: Table, where: object, order_by: object = None
) -> list[tuple[int, dict[str, Any]]]:
    """Rows that pass every condition, as (file row number, row), then sorted.

    Raises:
        InvalidFetchOptions: When a condition names an unknown column or operator.
    """
    conditions = where if isinstance(where, list) else ([where] if where else [])
    checks: list[tuple[str, str, Any]] = []

    for condition in conditions:
        if not isinstance(condition, dict):
            raise InvalidFetchOptions(
                "where must be a list of {column, op, value} objects"
            )

        name = table.column(condition.get("column"))
        op = str(condition.get("op") or "eq").lower()

        if name is None:
            raise InvalidFetchOptions(
                f"Unknown column {condition.get('column')!r}; "
                f"columns: {', '.join(table.columns)}"
            )

        if op not in _OPS:
            raise InvalidFetchOptions(f"Unknown op {op!r}; allowed: {', '.join(_OPS)}")

        checks.append((name, op, condition.get("value")))

    kept = [
        (index, row)
        for index, row in enumerate(table.rows)
        if all(_compare(row.get(name), op, value) for name, op, value in checks)
    ]

    if order_by:
        text = str(order_by).strip()
        descending = text.startswith("-")
        name = table.column(text.lstrip("-"))

        if name is None:
            raise InvalidFetchOptions(f"Unknown order_by column {order_by!r}")

        def sort_key(pair: tuple[int, dict[str, Any]]) -> tuple:
            cell = pair[1].get(name)
            number, when = parse_number(cell), parse_when(cell)

            if number is not None:
                return (0, number, "")

            if when:
                return (0, when.timestamp(), "")

            return (1, 0.0, str(cell or "").lower())

        kept.sort(key=sort_key, reverse=descending)

    return kept


# ---------------------------------------------------------------------------
# One measurement over time
# ---------------------------------------------------------------------------

_STATUS_RANK = {"corrected": 0, "amended": 0, "final": 1, "": 2, "preliminary": 3}


def _day_bound(value: object, name: str) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None

    when = parse_when(value)

    if when is None:
        raise InvalidFetchOptions(f"{name} must be a date such as 2011-03-10")

    return when


def series(
    table: Table,
    *,
    item: object = None,
    column: object = None,
    from_date: object = None,
    to_date: object = None,
) -> dict[str, Any]:
    """One measurement over time, in one unit, with its summary values.

    A long table is read by ``item`` (a test name, a key such as ``lab.afp``, or a
    LOINC code). A wide table is read by ``column``. A result that a corrected
    result replaced is left out and listed under ``superseded``. A value in a unit
    that cannot be converted is listed under ``other_units`` and kept out of the
    summary.

    Raises:
        InvalidFetchOptions: When the table has no date column, or the item or
            column is not in it.
    """
    start = _day_bound(from_date, "from_date")
    end = _day_bound(to_date, "to_date")

    if end is not None and len(str(to_date).strip()) <= 10:
        end = end.replace(hour=23, minute=59, second=59)

    if table.shape == "long":
        found = _long_points(table, item if item is not None else column)
    elif table.shape == "wide":
        found = _wide_points(table, column if column is not None else item)
    else:
        raise InvalidFetchOptions(
            f"series needs a table with a date column and measurements; "
            f"this table's shape is {table.shape!r}. Use view=rows."
        )

    points = [
        point
        for point in found["points"]
        if (start is None or point["_when"] >= start)
        and (end is None or point["_when"] <= end)
    ]
    points.sort(key=lambda point: (point["_when"], point["row"]))
    kept, superseded = _drop_superseded(points)
    numeric = [point for point in kept if "value" in point]
    result = {key: value for key, value in found.items() if key not in ("points",)}
    result["points"] = [_public(point) for point in numeric]
    result["stats"] = _stats(numeric)

    extras = {
        "text_values": [p for p in kept if "value" not in p and "text" in p],
        "other_units": [p for p in kept if "value" not in p and "text" not in p],
        "superseded": superseded,
    }

    for name, items in extras.items():
        if items:
            result[name] = [_public(point) for point in items]

    return result


def _public(point: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in point.items() if not key.startswith("_")}


def _long_points(table: Table, wanted: object) -> dict[str, Any]:
    roles = table.roles
    text = str(wanted or "").strip()

    if not text:
        raise InvalidFetchOptions(
            "series on this table needs item, such as item='AFP'. "
            "describe lists the items."
        )

    code_column = roles.get("code")

    def rows_for(wanted_name: str) -> tuple[Analyte | None, list]:
        analyte = find_analyte(wanted_name)
        found: list[tuple[int, dict[str, Any]]] = []

        for index, row in enumerate(table.rows):
            name = str(row.get(roles["item"]) or "").strip()
            code = row.get(code_column) if code_column else None

            if name.lower() == wanted_name.lower() or (
                code and str(code).strip() == wanted_name
            ):
                found.append((index, row))
            elif analyte and find_analyte(name, code) is analyte:
                found.append((index, row))

        return analyte, found

    known, matched = rows_for(text)

    if not matched:
        # A name given with its unit, such as "AFP [ng/mL]" or "AFP (ng/mL)".
        bare = re.sub(r"\s*[\[(].*$", "", text).strip()

        if bare and bare != text:
            known, matched = rows_for(bare)

    if not matched:
        names = ", ".join(entry["item"] for entry in table.items(40))
        raise InvalidFetchOptions(f"No item {text!r} in this table. Items: {names}")

    known = known or find_analyte(
        matched[0][1].get(roles["item"]),
        matched[0][1].get(code_column) if code_column else None,
    )
    reported = [str(row.get(roles.get("unit", "")) or "").strip() for _, row in matched]
    unit = known.unit if known else max(set(reported), key=reported.count)
    points = []

    for index, row in matched:
        when = parse_when(row.get(roles["time"]))

        if when is None:
            continue

        raw_unit = str(row.get(roles.get("unit", "")) or "").strip()
        factor = _factor(known, raw_unit, unit)
        point = _point(index, when, row.get(roles["value"]), factor, unit, raw_unit)

        for role in ("ref_low", "ref_high"):
            limit = parse_number(row.get(roles.get(role, "")))

            if limit is not None and factor is not None:
                point[role] = clean_number(round(limit * factor, 4))

        if "value" in point:
            point["flag"] = _flag(point, row.get(roles.get("flag", "")))

        status = str(row.get(roles.get("status", "")) or "").strip().lower()

        if status:
            point["status"] = status

        points.append(point)

    return {
        "item": known.label if known else str(matched[0][1].get(roles["item"])),
        "key": known.key if known else None,
        "unit": unit,
        "value_column": roles["value"],
        "time_column": roles["time"],
        "points": points,
    }


def _wide_points(table: Table, wanted: object) -> dict[str, Any]:
    name = table.column(wanted)

    if name is None or name not in table.measured_columns():
        raise InvalidFetchOptions(
            f"series on this table needs column, one of: "
            f"{', '.join(table.measured_columns())}"
        )

    unit = column_unit(name)
    points = []

    for index, row in enumerate(table.rows):
        when = parse_when(row.get(table.roles["time"]))

        if when is None or is_null(row.get(name)):
            continue

        points.append(_point(index, when, row.get(name), 1.0, unit, unit))

    return {
        "item": name,
        "key": COLUMN_KEYS.get(column_name_key(name)),
        "unit": unit,
        "value_column": name,
        "time_column": table.roles["time"],
        "points": points,
    }


def _factor(known: Analyte | None, raw_unit: str, unit: str) -> float | None:
    if known is None:
        return 1.0 if raw_unit == unit else None

    return known.factors.get(_unit_key(raw_unit))


def _point(
    index: int,
    when: datetime,
    raw: object,
    factor: float | None,
    unit: str,
    raw_unit: str,
) -> dict[str, Any]:
    point: dict[str, Any] = {
        "row": index,
        "date": when.date().isoformat(),
        "_when": when,
    }

    if when.hour or when.minute:
        point["time"] = when.strftime("%H:%M")

    number = parse_number(raw)

    if number is None:
        point["text"] = str(raw or "").strip()
        return point

    if factor is None:
        point["reported"] = {"value": clean_number(number), "unit": raw_unit}
        return point

    point["value"] = clean_number(round(number * factor, 4))
    point["unit"] = unit

    if factor != 1.0 or _unit_key(raw_unit) != _unit_key(unit):
        point["reported"] = {"value": clean_number(number), "unit": raw_unit}

    return point


def _flag(point: dict[str, Any], reported: object) -> str:
    """H or L from the reference range on the row, or the flag the lab reported."""
    value = point["value"]

    if "ref_high" in point and value > point["ref_high"]:
        return "H"

    if "ref_low" in point and value < point["ref_low"]:
        return "L"

    if "ref_high" in point or "ref_low" in point:
        return ""

    return str(reported or "").strip().upper()[:2]


def _drop_superseded(
    points: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep the best result per collection time; a corrected one beats the rest."""
    best: dict[datetime, int] = {}

    for point in points:
        rank = _STATUS_RANK.get(point.get("status", ""), 2)
        best[point["_when"]] = min(best.get(point["_when"], rank), rank)

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    for point in points:
        rank = _STATUS_RANK.get(point.get("status", ""), 2)
        (kept if rank == best[point["_when"]] else dropped).append(point)

    return kept, dropped


def _stats(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return {"n": 0}

    def brief(point: dict[str, Any]) -> dict[str, Any]:
        return {"date": point["date"], "value": point["value"], "row": point["row"]}

    first, last = points[0], points[-1]
    lowest = min(points, key=lambda point: point["value"])
    highest = max(points, key=lambda point: point["value"])
    stats: dict[str, Any] = {
        "n": len(points),
        "first": brief(first),
        "last": brief(last),
        "min": brief(lowest),
        "max": brief(highest),
    }

    if len(points) > 1:
        change = last["value"] - first["value"]
        stats["change"] = clean_number(round(change, 4))
        stats["days"] = (last["_when"].date() - first["_when"].date()).days

        if first["value"]:
            stats["pct_change"] = clean_number(
                round(100 * change / abs(first["value"]), 1)
            )

    for flag, name in (("H", "high"), ("L", "low")):
        flagged = [point for point in points if point.get("flag") == flag]

        if flagged:
            stats[name] = {"n": len(flagged), "first": brief(flagged[0])}

    return stats
