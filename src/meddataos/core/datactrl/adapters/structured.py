"""Structured table/record adapter."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from meddataos.core.datactrl.adapters.base import resolve_fetch_options
from meddataos.core.datactrl.errors import InvalidFetchOptions, ResourceNotFound
from meddataos.core.datactrl.limits import (
    DEFAULT_TABLE_LIMIT,
    MAX_SERIES_POINTS,
    MAX_TABLE_ITEMS,
    MAX_TABLE_LIMIT,
    MAX_TYPED_ROWS,
)
from meddataos.core.datactrl.types import (
    DescribeResult,
    FetchResult,
    Modality,
    ResourceRef,
    ViewSpec,
    make_resource_id,
)
from meddataos.core.datactrl.utils import scores
from meddataos.core.datactrl.utils.table import Table, filter_rows, series


@dataclass
class _TableRecord:
    ref: ResourceRef
    path: Path
    format: str
    # The typed table and the file time it was read at.
    table: Table | None = None
    read_at: float = 0.0
    # True when the file has more rows than the typed views read.
    partial: bool = False


_VIEWS = (
    ViewSpec(
        name="rows",
        description=(
            "Rows with limit/offset and column projection. where filters rows "
            "with {column, op, value} conditions (op: eq, ne, gt, ge, lt, le, "
            "contains, in) and order_by sorts them (prefix - for descending). "
            "row_numbers gives each returned row's position in the file."
        ),
        defaults={"limit": DEFAULT_TABLE_LIMIT, "offset": 0},
        max={"limit": MAX_TABLE_LIMIT},
    ),
    ViewSpec(
        name="series",
        description=(
            "One measurement over time in one unit, with first, last, lowest, "
            "highest, and change. Pass item for a table with one row per test "
            "(shape long), or column for a table with one row per visit (shape "
            "wide). from_date and to_date bound the dates."
        ),
    ),
    ViewSpec(
        name="score",
        description=(
            "A clinical score computed from this table: name (see describe), "
            "optional at date, and inputs for values the table does not hold. "
            "An input that is missing is listed, not estimated."
        ),
    ),
)


class StructuredAdapter:
    """Structured table/record modality adapter."""

    modality = Modality.STRUCTURED
    version = "1"

    def __init__(self) -> None:
        """Create an empty adapter; call ``ingest`` before describe/fetch."""
        self._tables: dict[str, _TableRecord] = {}

    def views(self) -> Sequence[ViewSpec]:
        """Return supported structured fetch views."""
        return _VIEWS

    def ingest(self, root: Path, paths: Sequence[Path]) -> list[ResourceRef]:
        """Index structured files under ``root`` as table resources."""
        self._tables.clear()
        refs: list[ResourceRef] = []
        for path in sorted(paths):
            rel = _rel(path, root)
            table_id = make_resource_id(Modality.STRUCTURED, _path_key(rel))
            fmt = path.suffix.lower().lstrip(".") or "csv"
            columns = _sample_columns(path, fmt)
            ref = ResourceRef(
                id=table_id,
                modality=Modality.STRUCTURED,
                path=rel,
                label=path.name,
                meta={"format": fmt, "columns": columns},
            )
            self._tables[table_id] = _TableRecord(ref=ref, path=path, format=fmt)
            refs.append(ref)
        return refs

    def describe(self, resource_id: str) -> DescribeResult:
        """Return columns, their types and roles, and what can be asked of the table.

        ``shape`` says how the table is laid out: ``long`` (one row per test per
        draw), ``wide`` (one row per visit), ``events``, ``intervals`` (start and
        stop dates), ``record`` (one row), or ``table``.
        """
        record = self._require(resource_id)
        table = self._table(record)
        columns = table.columns or list(record.ref.meta.get("columns", []))
        row_count = len(table.rows)

        if record.partial:
            _, row_count = _schema_and_count(record.path, record.format)

        summary: dict[str, Any] = {
            "format": record.format,
            "columns": columns,
            "row_count": row_count,
            "shape": table.shape,
            "roles": dict(table.roles),
            "column_types": table.types,
        }

        if table.shape == "long":
            summary["items"] = table.items(MAX_TABLE_ITEMS)
        elif table.shape == "wide":
            summary["series_columns"] = table.measured_columns()

        time_column = table.roles.get("time") or table.roles.get("start")

        if time_column:
            dated = table.types[time_column]
            summary["date_range"] = [dated.get("min"), dated.get("max")]

        if table.shape in ("long", "wide", "record"):
            summary["scores"] = scores.available()

        if record.partial:
            summary["rows_read"] = len(table.rows)
            summary["note"] = (
                f"Only the first {len(table.rows)} rows are typed, filtered, and "
                "read as series."
            )

        return DescribeResult(
            id=resource_id,
            modality=Modality.STRUCTURED,
            label=record.ref.label,
            path=record.ref.path,
            views=[spec.name for spec in _VIEWS],
            view_specs=list(_VIEWS),
            summary=summary,
        )

    def fetch(
        self, resource_id: str, view: str | None = None, **opts: Any
    ) -> FetchResult:
        """Fetch rows, one measurement over time, or a score."""
        resolved_view, merged = resolve_fetch_options(
            _VIEWS, view, opts, default_view="rows"
        )
        record = self._require(resource_id)
        table = self._table(record)
        bounds: dict[str, Any] = {}

        if resolved_view == "series":
            data = series(
                table,
                item=merged.get("item"),
                column=merged.get("column"),
                from_date=merged.get("from_date"),
                to_date=merged.get("to_date"),
            )
            total = len(data["points"])
            data["points"] = data["points"][-MAX_SERIES_POINTS:]
            bounds = {"points": total, "truncated": total > MAX_SERIES_POINTS}
        elif resolved_view == "score":
            data = scores.compute(
                table,
                merged.get("name"),
                at=merged.get("at"),
                inputs=merged.get("inputs"),
                window_days=merged.get("window_days"),
            )
        else:
            data, bounds = self._rows(record, table, merged)

        return FetchResult(
            id=resource_id,
            modality=Modality.STRUCTURED,
            view=resolved_view,
            data=data,
            bounds=bounds,
            provenance={
                "path": record.ref.path,
                "adapter": "structured",
                "adapter_version": self.version,
            },
        )

    def _rows(
        self, record: _TableRecord, table: Table, merged: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One page of rows, after any filter and sort."""
        limit = int(merged.get("limit", DEFAULT_TABLE_LIMIT))
        offset = int(merged.get("offset", 0))
        columns = self._projection(table, merged.get("columns"))
        found = filter_rows(table, merged.get("where"), merged.get("order_by"))
        page = found[offset : offset + limit]
        rows = [row for _, row in page]

        if columns:
            rows = [{key: row.get(key) for key in columns} for row in rows]

        data: dict[str, Any] = {
            "columns": columns
            or table.columns
            or list(record.ref.meta.get("columns", [])),
            "rows": rows,
            "row_count": len(table.rows),
            "row_numbers": [index for index, _ in page],
        }

        if record.partial:
            data["rows_read"] = len(table.rows)

        if merged.get("where"):
            data["matched"] = len(found)

        bounds = {
            "limit": limit,
            "offset": offset,
            "truncated": offset + limit < len(found),
        }
        return data, bounds

    @staticmethod
    def _projection(table: Table, wanted: Any) -> list[str] | None:
        """The table's own names for the columns asked for.

        Raises:
            InvalidFetchOptions: When a column is not in the table. A column of
                nulls would read as "not recorded" when the name was only wrong.
        """
        if not wanted:
            return None

        names = (
            [str(item) for item in wanted]
            if isinstance(wanted, list)
            else [str(wanted)]
        )
        found = [table.column(name) for name in names]
        missing = [
            name for name, column in zip(names, found, strict=True) if not column
        ]

        if missing:
            raise InvalidFetchOptions(
                f"Unknown column {', '.join(repr(name) for name in missing)}; "
                f"columns: {', '.join(table.columns)}"
            )

        return [column for column in found if column]

    def _table(self, record: _TableRecord) -> Table:
        """The typed table, read again when the file has changed."""
        try:
            modified = record.path.stat().st_mtime
        except OSError:
            modified = 0.0

        if record.table is None or modified != record.read_at:
            rows = _load_rows(record.path, record.format, max_rows=MAX_TYPED_ROWS + 1)
            record.partial = len(rows) > MAX_TYPED_ROWS
            rows = rows[:MAX_TYPED_ROWS]
            columns = (
                list(rows[0].keys())
                if rows
                else _sample_columns(record.path, record.format)
            )
            record.table = Table.build(columns, rows)
            record.read_at = modified

        return record.table

    def _require(self, resource_id: str) -> _TableRecord:
        """Look up an indexed table or raise ResourceNotFound."""
        try:
            return self._tables[resource_id]
        except KeyError as exc:
            raise ResourceNotFound(f"Unknown table: {resource_id!r}") from exc


def _sample_columns(path: Path, fmt: str) -> list[str]:
    """Read column names from the first row of a table file."""
    if fmt in {"csv", "tsv"}:
        delimiter = "," if fmt == "csv" else "\t"
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            return list(reader.fieldnames or [])
    if fmt == "jsonl":
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    return list(row.keys())
        return []
    rows = _load_rows(path, fmt, max_rows=1)
    return list(rows[0].keys()) if rows else []


def _schema_and_count(path: Path, fmt: str) -> tuple[list[str], int]:
    """Return (columns, row_count) without materializing every CSV/JSONL row."""
    if fmt in {"csv", "tsv"}:
        delimiter = "," if fmt == "csv" else "\t"
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            columns = list(reader.fieldnames or [])
            row_count = sum(1 for _ in reader)
        return columns, row_count
    if fmt == "jsonl":
        columns = []
        row_count = 0
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    if not columns:
                        columns = list(row.keys())
                    row_count += 1
        return columns, row_count
    rows = _load_rows(path, fmt)
    columns = list(rows[0].keys()) if rows else []
    return columns, len(rows)


def _load_rows(
    path: Path, fmt: str, max_rows: int | None = None
) -> list[dict[str, Any]]:
    """Load rows from a supported structured file format."""
    if fmt == "csv":
        return _load_csv(path, delimiter=",", max_rows=max_rows)
    if fmt == "tsv":
        return _load_csv(path, delimiter="\t", max_rows=max_rows)
    if fmt == "json":
        return _load_json(path, max_rows=max_rows)
    if fmt == "jsonl":
        return _load_jsonl(path, max_rows=max_rows)
    if fmt == "parquet":
        return _load_parquet(path, max_rows=max_rows)
    raise ValueError(f"Unsupported structured format: {fmt!r}")


def _load_csv(
    path: Path,
    *,
    delimiter: str,
    max_rows: int | None,
) -> list[dict[str, Any]]:
    """Load delimiter-separated rows from a CSV/TSV file."""
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        rows: list[dict[str, Any]] = []
        for row in reader:
            rows.append(dict(row))
            if max_rows is not None and len(rows) >= max_rows:
                break
        return rows


def _load_json(path: Path, max_rows: int | None) -> list[dict[str, Any]]:
    """Load tabular rows from a JSON array or ``{data: [...]}`` wrapper."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        rows = payload["data"]
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(f"JSON is not tabular: {path}")
    rows = [row for row in rows if isinstance(row, dict)]
    if max_rows is not None:
        rows = rows[:max_rows]
    return rows


def _load_jsonl(path: Path, max_rows: int | None) -> list[dict[str, Any]]:
    """Load object rows from a JSONL file."""
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
            if max_rows is not None and len(rows) >= max_rows:
                break
    return rows


def _load_parquet(path: Path, max_rows: int | None) -> list[dict[str, Any]]:
    """Load rows from a Parquet file (requires optional pyarrow extra)."""
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ValueError("Parquet support requires pyarrow") from exc
    table = pq.read_table(path)
    if max_rows is not None:
        table = table.slice(0, max_rows)
    return cast(list[dict[str, Any]], table.to_pylist())


def _rel(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root``, or its string form if outside."""
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _path_key(relative_path: str) -> str:
    """Hash a relative path into a stable structured resource key."""
    return hashlib.sha1(relative_path.encode("utf-8")).hexdigest()[:12]
