"""The dated things in the open case, in one lane per source.

Built from what datactrl discovered: note dates, DICOM study dates, and the date
columns of each table. No model is asked, and nothing is merged or corrected: a
note dated 2004 beside tables dated 2011 shows as exactly that.

"""

from __future__ import annotations

from typing import Any

from meddataos.core.datactrl.case import Case
from meddataos.core.datactrl.limits import MAX_TABLE_LIMIT
from meddataos.core.datactrl.utils.table import is_null, parse_when
from meddataos.core.verifier.paths import web_path

MAX_EVENTS_PER_LANE = 400

# Columns that say what a dated row is, in the order they are preferred.
_LABEL_COLUMNS = (
    "description",
    "encounter",
    "visit",
    "medication",
    "event",
    "procedure",
    "name",
    "type",
    "label",
    "fraction",
)


def build(opened: Case) -> dict[str, Any]:
    """Lanes of dated events for the page, with the first and last date."""
    report = opened.report
    lanes = [
        _lane("Notes", "note", _note_events(report.notes)),
        _lane("Imaging", "dicom", _imaging_events(report.dicom)),
    ]

    for ref in report.structured:
        try:
            events = _table_events(opened, ref)
        except Exception:  # noqa: BLE001 - a table that cannot be read has no lane
            continue

        name = ref.label.rsplit(".", 1)[0].replace("_", " ")
        lanes.append(_lane(name[:1].upper() + name[1:], "structured", events))

    lanes = [lane for lane in lanes if lane["events"]]
    days = [
        day
        for lane in lanes
        for event in lane["events"]
        for day in (event["date"], event.get("end"))
        if day
    ]
    return {
        "lanes": lanes,
        "range": [min(days), max(days)] if days else None,
    }


def _lane(name: str, modality: str, events: list[dict]) -> dict[str, Any]:
    events.sort(key=lambda event: event["date"])
    return {
        "name": name,
        "modality": modality,
        "events": events[:MAX_EVENTS_PER_LANE],
        "truncated": len(events) > MAX_EVENTS_PER_LANE,
    }


def _note_events(notes: list) -> list[dict]:
    events = []

    for ref in notes:
        day = _day(ref.meta.get("note_date"))

        if not day:
            continue

        event = {
            "date": day,
            "label": ref.label,
            "web_path": web_path(ref.path),
            "id": ref.id,
        }

        if ref.meta.get("note_type"):
            event["detail"] = str(ref.meta["note_type"]).replace("_", " ")

        # A date the note itself is unsure of is drawn as uncertain.
        if ref.meta.get("note_date_confidence") in ("low", "none") or ref.meta.get(
            "note_date_flags"
        ):
            event["uncertain"] = True

        events.append(event)

    return events


def _imaging_events(series: list) -> list[dict]:
    """One event per study date and modality, counting its series."""
    grouped: dict[tuple[str, str], dict] = {}

    for ref in series:
        day = _day(ref.meta.get("StudyDate"))

        if not day:
            continue

        modality = str(ref.meta.get("Modality") or "DICOM")
        event = grouped.setdefault(
            (day, modality), {"date": day, "label": modality, "count": 0}
        )
        event["count"] += 1

        if "web_path" not in event:
            event["web_path"] = web_path(ref.path)
            event["id"] = ref.id

        study = str(ref.meta.get("StudyDescription") or "").strip()

        if study and study.lower() not in ("nan", "none"):
            event["detail"] = study

    for event in grouped.values():
        count = event.pop("count")
        event["label"] = f"{event['label']} · {count} series"

    return list(grouped.values())


def _table_events(opened: Case, ref) -> list[dict]:
    summary = opened.describe(ref.id)["summary"]
    shape = summary.get("shape")
    roles = summary.get("roles") or {}
    time_column = roles.get("start") if shape == "intervals" else roles.get("time")

    if shape == "record" or not time_column:
        return []

    data = opened.fetch(ref.id, view="rows", limit=MAX_TABLE_LIMIT)["data"]
    path = web_path(ref.path)
    label_column = _label_column(summary)
    stop_column = roles.get("stop")
    # What kind of event a row is (procedure, clinic, imaging), when a column says.
    type_column = next(
        (
            name
            for name in summary.get("columns") or []
            if name.strip().lower() == "type" and name != label_column
        ),
        None,
    )
    events: list[dict] = []

    if shape == "long":
        # One event per draw, not one per result.
        draws: dict[str, int] = {}

        for row in data["rows"]:
            day = _day(row.get(time_column))

            if day:
                draws[day] = draws.get(day, 0) + 1

        return [
            {
                "date": day,
                "label": f"{count} result{'s' if count != 1 else ''}",
                "web_path": path,
                "id": ref.id,
            }
            for day, count in draws.items()
        ]

    for number, row in zip(data["row_numbers"], data["rows"], strict=False):
        day = _day(row.get(time_column))

        if not day:
            continue

        event = {
            "date": day,
            "label": _row_label(row, label_column, ref.label),
            "web_path": path,
            "id": ref.id,
            "row": number,
        }
        if type_column and str(row.get(type_column) or "").strip():
            event["detail"] = str(row[type_column]).strip()

        end = _day(row.get(stop_column)) if stop_column else None

        if end and end >= day:
            event["end"] = end
        elif shape == "intervals" and stop_column and is_null(row.get(stop_column)):
            event["open"] = True

        events.append(event)

    return events


def _row_label(row: dict, column: str | None, fallback: str) -> str:
    """What a dated row is called. A bare number is named by its column."""
    value = str(row.get(column) or "").strip() if column else ""

    if not value:
        return fallback

    return f"{column} {value}" if value.isdigit() else value


def _label_column(summary: dict) -> str | None:
    columns = list(summary.get("columns") or [])
    types = summary.get("column_types") or {}
    by_key = {name.strip().lower(): name for name in columns}

    for wanted in _LABEL_COLUMNS:
        if wanted in by_key:
            return str(by_key[wanted])

    for name in columns:
        kind = (types.get(name) or {}).get("type")
        distinct = (types.get(name) or {}).get("distinct", 0)

        if (
            kind in ("text", "category")
            and distinct > 1
            and not name.lower().endswith("id")
        ):
            return str(name)

    return None


def _day(value: object) -> str | None:
    when = parse_when(value)
    return when.date().isoformat() if when else None
