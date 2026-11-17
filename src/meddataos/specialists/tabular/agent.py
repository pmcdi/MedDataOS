"""Tabular specialist: a model plans which table reads to make, code does the rest.

The specialist is asked through the MedDataOS server, which runs its tool calls
and hands back each result with a cite token. One consult is two or three calls
to this module:

1. No reads yet. A model sees the catalog of the case's tables (columns, types,
   the tests a lab table holds, the scores on offer) and the question, and names
   the ``fetch`` calls to make. The numbers come from those calls: ``series``
   gives first, last, lowest, highest and change, and ``score`` computes Child-Pugh,
   ALBI, MELD, FIB-4, BMI, or an interval between dates.
2. The reads are back. A model writes one claim per finding and names the read
   and the cells each claim rests on. Code then drops any claim whose numbers
   are not in the read it names, builds the citations, and lists what a score
   still needs.

The model never does arithmetic, and nothing it writes is cited without a check.
The server checks the cited cells and computed values again.

"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meddataos.datactrl.utils.table import (
    COLUMN_KEYS,
    column_name_key,
    parse_number,
    parse_when,
)
from meddataos.specialists.common import REFUSAL_PREFIX, parse_request, user_turns

MAX_CALLS = 8
MAX_ROUNDS = 2
MAX_CLAIMS = 10
MAX_REFUSALS = 2
# Rows a planned rows read may ask for, so one reply stays readable.
MAX_ROWS = 60

_VIEW_ARGS = {
    "rows": ("where", "order_by", "columns", "limit", "offset"),
    "series": ("item", "column", "from_date", "to_date"),
    "score": ("name", "at", "inputs", "window_days"),
}

# How much a keyed value from each kind of table is trusted beside a note.
SOURCE_TYPES = {"long": "lab_table", "record": "registry"}
DEFAULT_SOURCE_TYPE = "chart_table"

_PLAN_PROMPT = """You are the tabular specialist for one patient's chart. You answer from the case's tables only.
Decide which table reads answer the question. You do not answer yet, and you never compute: every number must come out of a read.

Question: {question}
{context}
TABLES (id, shape, columns with types; for a lab table, the tests it holds):
{catalog}

Reads you can ask for. Each is one object in "calls":
- {{"table": "<id>", "view": "series", "item": "<test>"}} for a table of shape long: one test over time, in one unit, with each result's date, reference range and H/L flag, and first, last, min, max, change, and percent change. "item" is a test name exactly as quoted in the table's tests, without its unit. This is the read for any question about a lab value: a rows read of a lab table gives no flags and no summary.
- {{"table": "<id>", "view": "series", "column": "<column>"}} for a table of shape wide: one numeric column over time, with the same summary.
  Add "from_date" and "to_date" (YYYY-MM-DD) to bound either series. To get a lowest or highest value, or a change, inside a period, bound the series to that period.
- {{"table": "<id>", "view": "rows", "where": [{{"column": "...", "op": "eq|ne|gt|ge|lt|le|contains|in", "value": ...}}], "order_by": "<column or -column>", "columns": [...], "limit": 20}} for rows as they are: a registry row, a medication, an encounter, a fraction. Column names are the table's own, exactly as listed. A date value without a time covers that whole day.
- {{"table": "<id>", "view": "score", "name": "child_pugh|albi|meld|fib4|bmi|interval", "at": "YYYY-MM-DD", "inputs": {{...}}}} for a score. Lab inputs are read from the table at the draw nearest "at" (the latest draw when "at" is left out). Give "inputs" only for what the table cannot hold and the question or context states, such as {{"ascites": "none", "encephalopathy": "none"}} for child_pugh, {{"age": 71}} for fib4, or {{"from_date": "...", "to_date": "..."}} for interval. Do not guess an input: a score with a missing input comes back naming what it needs.

Rules:
- Ask for every read the question needs, at most {max_calls}. Several tests are several series reads.
- A question about a period (after surgery, during treatment) needs dates. If you do not know them, read the table that holds them (encounters, fractions, the registry row) as well.
- If no table can answer any part of the question, return no calls and say why in "nothing".

Return ONLY JSON: {{"calls": [...], "nothing": "<empty, or why the tables cannot answer>"}}"""

_ANSWER_PROMPT = """You are the tabular specialist for one patient's chart. The reads below came back from the case's tables.
Write the findings that answer the question, using only what the reads returned.

Question: {question}
{context}
READS (each has a token such as c3; a read that FAILED has none and says why):
{reads}

Return ONLY JSON:
{{"claims": [{{"text": "<one sentence, with the value, its unit, and its date>", "read": "<token>",
             "cells": [{{"row": <row number from that read>, "column": "<column, for a rows read>"}}],
             "computed": <true when the sentence's main value is a change, a percent change, a count, a number of days, or a score; false when it is read from a row>,
             "value": <for computed: that value exactly as the read gives it>, "unit": "<its unit or empty>"}}],
 "not_in_tables": ["<a part of the question the tables do not answer>"]{more}}}
Rules:
- Every number and date in a sentence must appear in the read it names. Do not add, subtract, average, or convert anything yourself. If the question needs a number no read gives, leave it out and list it under not_in_tables{more_rule}.
- Name the rows the sentence rests on in "cells". A series point's row is its "row". For a rows read, give the row number and the column.
- A lowest, highest, first, or last value is a row of the series: cite that row. Only a change, percent change, count, number of days, or score is computed.
- A score read that is not complete has no value: do not state one. Say nothing about it; the missing inputs are reported for you.
- A result marked superseded was replaced: report the value that replaced it, and mention the superseded one only if the question is about it.
- State an abnormal flag (H or L) or a reference range only as the read gives it.
- At most {max_claims} claims. No ids, tokens, or row numbers in the sentence text."""

_MORE = ',\n "more_calls": [<further reads, in the same form as before, only if the question cannot be answered without them>]'
_MORE_RULE = ', or ask for the read that gives it under more_calls'


@dataclass
class Read:
    """One fetch this specialist asked for, and what the server returned."""

    token: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    table: dict[str, Any] = field(default_factory=dict)

    @property
    def view(self) -> str:
        return str(self.result.get("view") or self.arguments.get("view") or "rows")

    @property
    def data(self) -> dict[str, Any]:
        raw = self.result.get("data")
        return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def catalog(case, resource_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """What each table of the case holds, as the planning model is shown it."""
    wanted = set(resource_ids or [])
    refs = list(case.report.structured)
    chosen = [ref for ref in refs if ref.id in wanted] or refs
    tables = []

    for ref in chosen:
        try:
            summary = case.describe(ref.id)["summary"]
        except Exception:  # noqa: BLE001 - a table that cannot be read is left out
            continue

        types = summary.get("column_types") or {}
        entry: dict[str, Any] = {
            "id": ref.id,
            "file": ref.path,
            "shape": summary.get("shape"),
            "rows": summary.get("row_count"),
            "roles": summary.get("roles") or {},
            "columns": {
                name: _column_hint(types.get(name) or {})
                for name in summary.get("columns") or []
            },
        }

        for key in ("items", "series_columns", "date_range"):
            if summary.get(key):
                entry[key] = summary[key]

        if summary.get("scores"):
            entry["scores"] = [score["name"] for score in summary["scores"]]

        tables.append(entry)

    return tables


def _column_hint(profile: dict[str, Any]) -> str:
    kind = str(profile.get("type") or "")

    if kind in ("number", "date") and "min" in profile:
        return f"{kind} {profile['min']}..{profile['max']}"

    if kind == "category":
        return "one of: " + ", ".join(str(value) for value in profile.get("values", []))

    return kind


def _catalog_text(tables: list[dict[str, Any]]) -> str:
    lines = []

    for table in tables:
        lines.append(
            f"{table['id']} | {table['file']} | shape {table['shape']} | "
            f"{table['rows']} rows"
            + (f" | dates {table['date_range'][0]} to {table['date_range'][1]}"
               if table.get("date_range") else "")
        )
        lines.append("  columns: " + "; ".join(
            f"{name} ({hint})" for name, hint in table["columns"].items()
        ))

        if table.get("items"):
            # The name is quoted: it is what a series read takes as "item".
            lines.append("  tests (name: unit, results, first to last date): " + "; ".join(
                f"\"{item['item']}\": {item.get('unit') or 'no unit'}, {item['n']}, "
                f"{item.get('first', '?')} to {item.get('last', '?')}"
                for item in table["items"]
            ))

        if table.get("series_columns"):
            lines.append("  numeric columns: " + ", ".join(table["series_columns"]))

        if table.get("scores"):
            lines.append("  scores: " + ", ".join(table["scores"]))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# One step of a consult
# ---------------------------------------------------------------------------


def next_message(
    messages: list[dict],
    case,
    generate_json: Callable[[str], Any],
) -> dict[str, Any]:
    """The specialist's next chat message: tool calls for more reads, or the reply.

    ``messages`` is the consult so far as the server sends it: the question,
    then this specialist's earlier tool calls and their results.
    """
    users = user_turns(messages)
    request = parse_request(users[0] if users else "")
    reads, failed = _reads(messages, case)
    rounds = sum(1 for item in messages if item.get("tool_calls"))
    refusals = [text for text in users if text.startswith(REFUSAL_PREFIX)]
    tables = catalog(case, request.resource_ids)

    if not tables:
        return _content(_envelope(
            "This case has no tables.", abstentions=["The case has no tables."]
        ))

    context = (
        f"Context from other specialists:\n{request.context}\n" if request.context else ""
    )

    if rounds == 0:
        plan = _as_dict(generate_json(_PLAN_PROMPT.format(
            question=request.question,
            context=context,
            catalog=_catalog_text(tables),
            max_calls=MAX_CALLS,
        )))
        calls = _tool_calls(plan.get("calls"), tables, 0)

        if calls:
            return {"role": "assistant", "content": None, "tool_calls": calls}

        why = str(plan.get("nothing") or "").strip() or (
            "The tables do not hold what this question asks for."
        )
        return _content(_envelope(why, abstentions=[why]))

    if len(refusals) > MAX_REFUSALS:
        return _content(
            "The tabular specialist could not produce a checked answer: "
            + refusals[-1][len(REFUSAL_PREFIX):].strip()
        )

    can_read_more = rounds < MAX_ROUNDS and not refusals
    payload = _as_dict(generate_json(_ANSWER_PROMPT.format(
        question=request.question,
        context=context,
        reads=_reads_text(reads, failed),
        max_claims=MAX_CLAIMS,
        more=_MORE if can_read_more else "",
        more_rule=_MORE_RULE if can_read_more else "",
    ) + (
        f"\n\nYour previous answer was refused: {refusals[-1]}" if refusals else ""
    )))

    if can_read_more:
        calls = _tool_calls(payload.get("more_calls"), tables, len(reads) + len(failed))

        if calls:
            return {"role": "assistant", "content": None, "tool_calls": calls}

    return _content(compose(payload, reads))


def _content(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tool_calls(raw: Any, tables: list[dict], made: int) -> list[dict]:
    """Planned reads as fetch tool calls, keeping only ones the catalog allows."""
    by_name: dict[str, dict] = {}

    for table in tables:
        by_name[table["id"]] = table
        by_name[table["file"].lower()] = table
        by_name[table["file"].rsplit("/", 1)[-1].lower()] = table

    calls = []

    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue

        name = str(item.get("table") or "")
        table = by_name.get(name) or by_name.get(name.lower())
        view = str(item.get("view") or "rows").lower()

        if table is None or view not in _VIEW_ARGS:
            continue

        arguments: dict[str, Any] = {"resource_id": table["id"], "view": view}

        for key in _VIEW_ARGS[view]:
            if item.get(key) not in (None, "", [], {}):
                arguments[key] = item[key]

        if view == "rows":
            limit = parse_number(arguments.get("limit"))
            arguments["limit"] = int(min(limit or MAX_ROWS, MAX_ROWS))

        calls.append({
            "id": f"tab_{made + index}",
            "type": "function",
            "function": {"name": "fetch", "arguments": json.dumps(arguments)},
        })

    return calls[:MAX_CALLS]


def _reads(messages: list[dict], case) -> tuple[list[Read], list[tuple[dict, str]]]:
    """The fetches that came back with a cite token, and the ones that failed.

    A failed fetch is kept with its arguments and the tool's message, so the
    model can ask for it again correctly.
    """
    asked: dict[str, dict] = {}

    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}

            try:
                asked[call.get("id", "")] = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                asked[call.get("id", "")] = {}

    tables = {table["id"]: table for table in catalog(case)}
    reads = []
    failed: list[tuple[dict, str]] = []

    for message in messages:
        if message.get("role") != "tool":
            continue

        try:
            payload = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError:
            continue

        if not isinstance(payload, dict):
            continue

        arguments = asked.get(message.get("tool_call_id", ""), {})

        if payload.get("status") == "error":
            failed.append((arguments, str(payload.get("message") or "the read failed")))
            continue

        token = payload.get("cite")
        result = payload.get("result")

        if not isinstance(token, str) or not isinstance(result, dict):
            continue

        reads.append(Read(
            token=token,
            arguments=arguments,
            result=result,
            table=tables.get(str(arguments.get("resource_id") or result.get("id")), {}),
        ))

    return reads, failed


def _reads_text(reads: list[Read], failed: list[tuple[dict, str]] | None = None) -> str:
    """Each read as the answering model sees it: compact JSON under its token."""
    blocks = [
        "FAILED "
        + json.dumps({k: v for k, v in arguments.items() if k != "resource_id"})
        + f": {message}"
        for arguments, message in failed or []
    ]

    if not reads and not blocks:
        return "(no read came back)"

    for read in reads:
        data = dict(read.data)

        if read.view == "rows":
            data = {
                "rows": [
                    {"row": number, **row}
                    for number, row in zip(
                        data.get("row_numbers") or [], data.get("rows") or [], strict=False
                    )
                ],
                "matched": data.get("matched", data.get("row_count")),
                "truncated": (read.result.get("bounds") or {}).get("truncated", False),
            }
        elif read.view == "score":
            data.pop("formula", None)

        asked = {
            key: value for key, value in read.arguments.items() if key != "resource_id"
        }
        blocks.append(
            f"[{read.token}] {read.table.get('file', 'table')} {json.dumps(asked)}\n"
            + json.dumps(data, ensure_ascii=False)
        )

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Checking the claims and building the reply
# ---------------------------------------------------------------------------

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
# Digits that are part of a unit, such as 10^3/uL or mL/min/1.73m2.
_UNIT_NUMBERS = re.compile(r"10\^\d+(?:/\w+)?|/?\s?1\.73\s?m2?")
_NUMBER = re.compile(r"(?<![\w.])[-+−]?\d+(?:,\d{3})*(?:\.\d+)?")


def _collect(node: Any, numbers: list[float], text: list[str]) -> None:
    """Every number and string a read returned. Row numbers are not values."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in ("row", "row_numbers", "window_days"):
                _collect(value, numbers, text)
        return

    if isinstance(node, list):
        for item in node:
            _collect(item, numbers, text)
        return

    number = parse_number(node)

    if number is not None:
        numbers.append(number)
    elif isinstance(node, str):
        text.append(node)
        # Numbers inside a label such as "Grade 1" count. The parts of a date do not.
        numbers.extend(
            float(found.replace(",", "").replace("−", "-"))
            for found in _NUMBER.findall(_strip_dates(node))
        )


def _strip_dates(text: str) -> str:
    return _UNIT_NUMBERS.sub(" ", _TIME.sub(" ", _DATE.sub(" ", text)))


def _returned(value: Any, read: Read) -> bool:
    """Whether a read returned this value, allowing for rounding."""
    numbers: list[float] = []
    strings: list[str] = []
    _collect(read.data, numbers, strings)
    number = parse_number(value)

    if number is None:
        return str(value).strip().casefold() in {text.strip().casefold() for text in strings}

    text = str(value).strip()
    decimals = len(text.split(".", 1)[1]) if "." in text else 0
    return any(_rounds_to(number, found, decimals) for found in numbers)


def _rounds_to(given: float, held: float, decimals: int) -> bool:
    """Whether ``given`` is ``held`` rounded to that many decimals.

    Rounding may not move the value by more than 5 percent, so 212 stands for
    211.6 but 3 does not stand for 2.78.
    """
    gap = abs(given - held)
    return gap <= 1e-9 or (
        gap <= 0.5 * 10**-decimals + 1e-9 and gap <= 0.05 * abs(held)
    )


def unsupported(sentence: str, read: Read) -> list[str]:
    """Numbers and dates in a sentence that its read did not return.

    A number may be rounded from the returned one, and its sign may be put in
    words ("fell by 3.4" for -3.4).
    """
    numbers: list[float] = []
    strings: list[str] = []
    _collect({"data": read.data, "asked": read.arguments}, numbers, strings)
    haystack = " ".join(strings)
    missing = [day for day in _DATE.findall(sentence) if day not in haystack]

    for found in _NUMBER.findall(_strip_dates(sentence)):
        text = found.replace(",", "").replace("−", "-")
        value = float(text)
        decimals = len(text.split(".", 1)[1]) if "." in text else 0

        if not any(
            _rounds_to(abs(value), abs(candidate), decimals) for candidate in numbers
        ):
            missing.append(found)

    return missing


def _cells(claim: dict, read: Read) -> list[dict]:
    """The cells a claim names that its read returned, as the citation sends them."""
    cells = []

    for item in claim.get("cells") or []:
        if not isinstance(item, dict):
            continue

        row = parse_number(item.get("row"))

        if row is None:
            continue

        number = int(row)

        if read.view == "series":
            point = _series_point(read, number)

            if point is not None:
                cells.append({"row": number})
        elif read.view == "rows":
            found = _row(read, number)
            column = next(
                (
                    name for name in (found or {})
                    if column_name_key(name) == column_name_key(item.get("column"))
                ),
                None,
            )

            # An empty cell is not evidence of anything.
            if column is not None and str((found or {}).get(column) or "").strip():
                cells.append({"row": number, "column": column})

    unique: list[dict] = []

    for cell in cells:
        if cell not in unique:
            unique.append(cell)

    return unique


def _series_point(read: Read, row: int) -> dict | None:
    for name in ("points", "text_values", "other_units", "superseded"):
        for point in read.data.get(name) or []:
            if isinstance(point, dict) and point.get("row") == row:
                return point

    return None


def _row(read: Read, row: int) -> dict | None:
    numbers = read.data.get("row_numbers") or []
    rows = read.data.get("rows") or []
    return rows[numbers.index(row)] if row in numbers else None


def compose(payload: dict[str, Any], reads: list[Read]) -> str:
    """The reply the server checks: verified claims, their citations, and the rest.

    A claim is kept when its read exists, every number and date in it is in that
    read, and it names cells of that read (or, when computed, a value the read
    returned).
    """
    by_token = {read.token: read for read in reads}
    kept: list[tuple[dict, Read, list[dict]]] = []
    abstentions = [
        item.strip() for item in payload.get("not_in_tables") or []
        if isinstance(item, str) and item.strip()
    ]

    for claim in (payload.get("claims") or [])[:MAX_CLAIMS]:
        if not isinstance(claim, dict):
            continue

        text = str(claim.get("text") or "").strip().rstrip(".")
        read = by_token.get(str(claim.get("read") or ""))

        if not text or read is None or unsupported(text, read):
            continue

        if read.view == "score" and not read.data.get("complete"):
            continue

        cells = _cells(claim, read)
        computed = bool(claim.get("computed")) or read.view == "score"

        if not computed and not cells:
            continue

        # A computed value from a series is one its summary gives, such as change.
        if (
            computed
            and read.view != "score"
            and (claim.get("value") in (None, "") or not _returned(claim["value"], read))
        ):
            continue

        kept.append(({**claim, "text": text, "computed": computed}, read, cells))

    needs = _needs(reads)

    if not kept:
        text = "The tables did not yield a checked answer to this question."

        if needs:
            text = (
                "A score could not be computed from the tables alone. It needs: "
                + "; ".join(need["what"] for need in needs) + "."
            )

        return _envelope(text, abstentions=abstentions, needs=needs)

    response = " ".join(f"{claim['text']} [cite:{read.token}]." for claim, read, _ in kept)
    citations = []
    facts: list[dict] = []

    for read in reads:
        mine = [(claim, cells) for claim, owner, cells in kept if owner is read]

        if not mine:
            continue

        citations.append(_citation(read, mine))
        facts.extend(_facts(read, mine))

    return _envelope(
        response, citations=citations, abstentions=abstentions, needs=needs, facts=facts
    )


def _envelope(response: str, **parts: list) -> str:
    body: dict[str, Any] = {"response": response, "citations": parts.pop("citations", [])}

    for name, value in parts.items():
        if value:
            body[name] = value

    return json.dumps(body, ensure_ascii=False)


def _needs(reads: list[Read]) -> list[dict]:
    """What an incomplete score still lacks, once each."""
    needs: list[dict] = []

    for read in reads:
        if read.view != "score" or read.data.get("complete"):
            continue

        for item in read.data.get("missing") or []:
            need = {"what": str(item.get("ask")), "why": str(read.data.get("title"))}

            if need not in needs:
                needs.append(need)

    return needs


def _citation(read: Read, claims: list[tuple[dict, list[dict]]]) -> dict[str, Any]:
    cells: list[dict] = []

    for _, found in claims:
        cells.extend(cell for cell in found if cell not in cells)

    computed = next((claim for claim, _ in claims if claim["computed"]), None)
    summary = " ".join(f"{claim['text']}." for claim, _ in claims[:3])
    label = read.data.get("title") or read.data.get("item") or read.table.get("file")

    if computed is None:
        citation: dict[str, Any] = {
            "kind": "table",
            "cite": read.token,
            "summary": summary,
            "cells": cells,
        }
        fields = sorted({cell["column"] for cell in cells if cell.get("column")})

        if read.view == "series" and read.data.get("value_column"):
            fields = [read.data["value_column"]]

        if fields:
            citation["fields"] = fields

        return citation

    if read.view == "score":
        value, unit = read.data.get("value"), read.data.get("unit") or None
        method = str(read.data.get("formula") or "")
        inputs = [
            {
                "cite": read.token,
                "label": str(component["input"]),
                "detail": _component_detail(component),
            }
            for component in read.data.get("components") or []
        ]
    else:
        value = computed.get("value")
        unit = str(computed.get("unit") or "") or read.data.get("unit") or None
        method = (
            "Read from the summary the table tool computed for this series: first, "
            "last, lowest and highest value, change, percent change, and counts."
        )
        inputs = [{"cite": read.token, "label": f"{label} series"}]

    citation = {
        "kind": "derived",
        "cite": read.token,
        "method": method,
        "summary": summary,
        "result": {"value": value, "unit": unit},
        "inputs": inputs or [{"cite": read.token, "label": str(label)}],
    }

    if cells:
        citation["cells"] = cells

    return citation


def _component_detail(component: dict) -> str:
    source = component.get("source")
    value = f"{component.get('value')} {component.get('unit') or ''}".strip()

    if isinstance(source, dict):
        where = f"row {source.get('row')}"

        if source.get("date"):
            where += f", {source['date']}"

        return f"{value} ({where})"

    return f"{value} (given with the question, not read from the table)"


def _facts(read: Read, claims: list[tuple[dict, list[dict]]]) -> list[dict]:
    """Keyed values behind the cited cells, for comparison with other sources."""
    source_type = SOURCE_TYPES.get(str(read.table.get("shape")), DEFAULT_SOURCE_TYPE)
    label = str(read.table.get("file") or "table").rsplit("/", 1)[-1]
    facts: list[dict] = []

    def add(key: str | None, value: Any, unit: Any, day: Any) -> None:
        if not key or value in (None, ""):
            return

        fact = {
            "key": key,
            "value": value,
            "cite": read.token,
            "source_type": source_type,
            "label": label,
        }

        if unit:
            fact["unit"] = unit

        if day:
            fact["date"] = day

        if fact not in facts:
            facts.append(fact)

    # A one-row record's date columns (RT start, last follow-up) are fields of
    # the record, not the date each of its other fields was measured.
    time_column = None

    if read.table.get("shape") != "record":
        time_column = (read.table.get("roles") or {}).get("time")

    for _, cells in claims:
        for cell in cells:
            if read.view == "series":
                point = _series_point(read, cell["row"]) or {}
                add(
                    read.data.get("key"),
                    point.get("value", point.get("text")),
                    point.get("unit"),
                    point.get("date"),
                )
            else:
                row = _row(read, cell["row"]) or {}
                when = parse_when(row.get(time_column)) if time_column else None
                add(
                    COLUMN_KEYS.get(column_name_key(cell["column"])),
                    row.get(cell["column"]),
                    None,
                    when.date().isoformat() if when else None,
                )

    return facts
