"""The citation contract: the rule shown to the agent, and the checks behind it.

Each ``[cite:]`` marker must name a read from this question; a quote must be in
the note it cites; a table value must be the cell it names; a number in a
sentence must be one the cited calls returned. Nothing here knows about HTTP or
the server's sessions: the calls of a question come in as a dict.
"""

from __future__ import annotations

import re
from typing import ClassVar

from meddataos.core.datactrl.utils import provenance
from meddataos.core.datactrl.utils.table import parse_number
from meddataos.core.verifier import schema
from meddataos.core.verifier.paths import note_span_url, path_key


class Contract:
    """The citation deal shown to the agent, plus the checks and filled fields."""

    MAX_QUOTE = 280

    RULE = (
        "After describe or fetch, the reply is one JSON object that matches the answer"
        "schema. "
        "Each citation's kind follows from the call its cite token came from: "
        "imaging for a DICOM series, note for a clinical note, table for a table. "
        "When the claim is a calculation or prediction from that call (for example a "
        "voxel volume from a volume fetch), use kind derived with a plain-language "
        "method, a result value/unit when you have one, and inputs: each item is "
        "the cite token of a call from this question plus what was taken from it. "
        "Put [cite:token] right after the words that call supports. "
        "A sentence may cite several calls, but never put markers side by side "
        "or group them at the end of the answer. "
        "A marker may not be the whole sentence. "
        "Do not send id, file, or web_path on a cited call; the server fills those in."
    )

    EXAMPLE: ClassVar[dict] = schema.EXAMPLE

    _MARKER = re.compile(r"\[cite:([^\]]+)\]")
    _ADJACENT = re.compile(r"\[cite:[^\]]+\]\s*[,;]?\s*\[cite:[^\]]+\]")

    # -----------------------------------------------------------------------
    # Prompt
    # -----------------------------------------------------------------------

    @classmethod
    def specialist_rule(cls) -> str:
        """Citation rule for a specialist that may cite a file it did not fetch."""
        return (
            "The reply may be plain text. "
            f"{cls.RULE} "
            "If you did not call describe or fetch, each citation has kind file, an "
            "id, file, and web_path, and web_path must be a case file. "
            "The marker is then [cite:] plus that id."
        )

    # -----------------------------------------------------------------------
    # Checks
    # -----------------------------------------------------------------------

    @staticmethod
    def has_marker(response: str) -> bool:
        """Whether the reply contains a [cite:] marker."""
        return Contract._MARKER.search(response) is not None

    @staticmethod
    def marker_problem(response: str) -> str | None:
        """A marker follows the words it supports, and markers are not adjacent."""
        if clump := Contract._ADJACENT.search(response):
            return (
                f"The markers {clump.group(0)!r} sit side by side; "
                "put each one right after the words that call supports."
            )

        protected = Contract._MARKER.sub(
            lambda match: match.group(0).replace(".", "\0"), response
        )
        sentences = [
            part.replace("\0", ".").strip() for part in re.split(r"[.!?]+", protected)
        ]

        for sentence in sentences:
            ids = Contract._MARKER.findall(sentence)

            if ids and not Contract._MARKER.sub("", sentence).strip():
                return (
                    f"[cite:{ids[0]}] stands alone; "
                    "put it right after the words it supports."
                )

        return None

    @staticmethod
    def citation_problem(
        citation: object,
        response: str,
        calls: dict[str, dict],
        paths: dict[str, str],
    ) -> str | None:
        """Why this citation can't back the answer, or None."""
        if not isinstance(citation, dict):
            return "a citation is not an object"

        if not isinstance(citation.get("summary"), str):
            return 'a citation needs a "summary" string'

        handle = citation.get("cite")
        marker = handle if isinstance(handle, str) and handle else citation.get("id")

        if not isinstance(marker, str) or not re.fullmatch(r"\S+", marker):
            return f"citation marker {marker!r} must be a string with no spaces"

        if f"[cite:{marker}]" not in response:
            return f"[cite:{marker}] does not appear in the response"

        call = calls.get(handle) if isinstance(handle, str) else None
        notes = Contract._notes_problem(
            citation, marker, call if isinstance(call, dict) else None
        )

        if notes:
            return notes

        if isinstance(handle, str) and handle:
            if handle not in calls:
                return (
                    f"citation cite {handle!r} is not a tool result from this question"
                )

            cells = Contract._cells_problem(citation, marker, calls[handle])

            if cells:
                return cells

            if citation.get("kind") == "derived":
                return Contract._derived_inputs_problem(
                    citation, marker, calls
                ) or Contract._derived_value_problem(citation, marker, calls)

            return None

        web = citation.get("web_path")

        if path_key(web) in paths:
            if not isinstance(citation.get("file"), str) or not citation["file"]:
                return f'citation {marker} needs a "file" name'
            return None

        fetched = {path_key(call.get("web_path")) for call in calls.values()}

        if path_key(web) in fetched:
            return (
                f"citation {marker} must use the cite token for that call, "
                f"not web_path {web!r}"
            )

        if calls:
            return (
                f"citation {marker} needs the cite token from the tool result it uses"
            )

        return f"citation {marker} web_path {web!r} is not an opened file"

    @staticmethod
    def _derived_inputs_problem(
        citation: dict, marker: str, calls: dict[str, dict]
    ) -> str | None:
        """Why a derived citation's inputs do not name calls from this question."""
        inputs = citation.get("inputs")

        if not isinstance(inputs, list) or not inputs:
            return f"citation {marker} needs inputs naming the calls the value used"

        for item in inputs:
            if not isinstance(item, dict):
                return f"citation {marker} input must be an object"

            token = item.get("cite")

            if not isinstance(token, str) or token not in calls:
                return (
                    f"citation {marker} input cite {token!r} is not a tool result "
                    "from this question"
                )

            if not isinstance(item.get("label"), str) or not item["label"].strip():
                return (
                    f"citation {marker} input {token} needs a label for the data used"
                )

        return None

    # -----------------------------------------------------------------------
    # Tables
    # -----------------------------------------------------------------------

    @staticmethod
    def _same_cell(given: object, actual: object) -> bool:
        """Whether a cited value is the value the table holds."""
        left, right = parse_number(given), parse_number(actual)

        if left is not None and right is not None:
            return abs(left - right) < 1e-9

        return str(given).strip().casefold() == str(actual).strip().casefold()

    @staticmethod
    def _series_point(table: dict, row: int) -> dict | None:
        """The point a series fetch returned for this file row."""
        for name in ("points", "text_values", "other_units", "superseded"):
            for point in table.get(name) or []:
                if isinstance(point, dict) and point.get("row") == row:
                    return point

        return None

    @staticmethod
    def _cell(table: dict, cell: dict) -> tuple[dict | None, str | None]:
        """The checked cell a citation names, or the reason it cannot be used."""
        row = Contract.as_int(cell.get("row"))
        column = cell.get("column")
        given = cell.get("value")

        if row is None:
            return None, "a cell needs the row number the fetch gave"

        if table.get("view") == "rows":
            found = (table.get("rows") or {}).get(str(row))

            if not isinstance(found, dict):
                return None, f"row {row} is not a row that fetch returned"

            name = next(
                (
                    key
                    for key in found
                    if isinstance(column, str)
                    and key.strip().casefold() == column.strip().casefold()
                ),
                None,
            )

            if name is None:
                return None, (
                    f"row {row} has no column {column!r}; columns: {', '.join(found)}"
                )

            actual = found[name]

            if actual is None or str(actual).strip() == "":
                return None, f"row {row} {name} is empty"

            if given is not None and not Contract._same_cell(given, actual):
                return None, f"row {row} {name} is {actual!r}, not {given!r}"

            return {"row": row, "column": name, "value": actual}, None

        point = Contract._series_point(table, row)

        if point is None:
            return None, f"row {row} is not a point that series returned"

        reported = point.get("reported")
        held = [
            point.get("value"),
            point.get("text"),
            reported.get("value") if isinstance(reported, dict) else None,
        ]
        held = [value for value in held if value is not None]

        if given is not None and not any(
            Contract._same_cell(given, value) for value in held
        ):
            return None, f"row {row} holds {held[0]!r}, not {given!r}"

        checked = {
            "row": row,
            "column": table.get("value_column"),
            "value": held[0] if held else None,
            "date": point.get("date"),
        }

        if point.get("unit"):
            checked["unit"] = point["unit"]

        return checked, None

    @staticmethod
    def _cells_problem(citation: dict, marker: str, call: dict) -> str | None:
        """Why the cells on this citation are not what the call returned, or None."""
        cells = citation.get("cells")

        if cells is None:
            return None

        if not isinstance(cells, list):
            return f"citation {marker} cells must be a list"

        if not cells:
            return None

        raw = call.get("table")
        table = raw if isinstance(raw, dict) else {}

        if table.get("view") not in ("rows", "series"):
            return (
                f"citation {marker} names cells, but its call is not a rows or "
                "series fetch of a table"
            )

        for cell in cells:
            if not isinstance(cell, dict):
                return f"citation {marker} cell must be an object"

            _, problem = Contract._cell(table, cell)

            if problem:
                return f"citation {marker} cell: {problem}"

        return None

    @staticmethod
    def returned_by(call: object) -> dict | None:
        """What a call returned that cited numbers are checked against, or None.

        A table read keeps its rows, series or score; a derived call such as exec
        keeps its printed output. Other calls (notes, images) return nothing a
        number can be checked against here.
        """
        if not isinstance(call, dict):
            return None

        for key in ("table", "output"):
            returned = call.get(key)

            if isinstance(returned, dict) and returned:
                return returned

        return None

    @staticmethod
    def returned(value: object, tables: list[dict]) -> bool:
        """Whether a table call returned this value, allowing for rounding.

        2.78 matches a returned 2.7849. Rounding may not move the value by more
        than 5 percent, so 3 does not match 2.78. A string has to be one the call
        returned.
        """
        return provenance.returned(value, tables)

    # Sentence ends: a stop before whitespace, or a line break. A decimal point
    # has a digit after it, so "2.4 mg/dL" stays inside its sentence.
    _SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")

    @staticmethod
    def sentence_numbers_problem(response: str, calls: dict[str, dict]) -> str | None:
        """A number or date that the calls a sentence cites did not return.

        Only sentences whose every marker is a call with recorded output (a table
        read, or an exec run) are judged. One that also cites a note, an image, a
        file or a finding may take its numbers from there.
        """
        for sentence in Contract._SENTENCE_END.split(response):
            tokens = list(dict.fromkeys(Contract._MARKER.findall(sentence)))

            if not tokens:
                continue

            cited = [calls.get(token) for token in tokens]
            outputs = [Contract.returned_by(call) for call in cited]

            if any(output is None for output in outputs):
                continue

            # What was asked counts too, such as the date a score was taken at.
            returned = [
                {"data": output, "asked": (call or {}).get("arguments") or {}}
                for call, output in zip(cited, outputs, strict=True)
            ]
            words = Contract._MARKER.sub(" ", sentence).strip()
            missing = provenance.unsupported(words, returned)

            if missing:
                return (
                    f"The sentence {words!r} gives {', '.join(missing)}, which no "
                    "call it cites returned. A number or date has to be one a fetch "
                    "returned (a cell, a series statistic, a score) or exec printed; "
                    "fetch or compute it, or leave it out."
                )

        return None

    @staticmethod
    def _derived_value_problem(
        citation: dict, marker: str, calls: dict[str, dict]
    ) -> str | None:
        """A derived value from a table has to be one a table call returned."""
        tokens = [citation.get("cite")] + [
            item.get("cite")
            for item in citation.get("inputs") or []
            if isinstance(item, dict)
        ]
        tables = [
            returned
            for token in dict.fromkeys(tokens)
            if isinstance(token, str)
            and (returned := Contract.returned_by(calls.get(token))) is not None
        ]

        if not tables:
            return None

        raw = citation.get("result")
        value = raw.get("value") if isinstance(raw, dict) else None

        if value is None or str(value).strip() == "":
            return (
                f"citation {marker} needs result.value: the value its series or "
                "score fetch returned"
            )

        if Contract.returned(value, tables):
            return None

        return (
            f"citation {marker} result {value!r} is not a value its call returned. "
            "A number from a table comes from a series or score fetch (first, "
            "last, min, max, change, a score), and a computed one from what exec "
            "printed, not from arithmetic on cells"
        )

    @staticmethod
    def _notes_problem(
        citation: dict, marker: str, call: dict | None = None
    ) -> str | None:
        """Why the quotes or discrepancies on this citation are unusable, or None."""
        evidence = citation.get("evidence", None)
        fetched = Contract._fetched_text(call)

        if evidence is not None and not isinstance(evidence, list):
            return f"citation {marker} evidence must be a list"

        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    return f"citation {marker} evidence item must be an object"

                if (
                    fetched is None
                    and Contract._names_offsets(item)
                    and Contract._span_offsets(item) is None
                ):
                    return (
                        f"citation {marker} evidence start and end must be offsets "
                        "with end greater than start"
                    )

                if fetched is not None:
                    slice_problem = Contract._quote_problem(marker, item, fetched, call)

                    if slice_problem:
                        return slice_problem

        if fetched is not None and not evidence:
            return (
                f"citation {marker} must include evidence with a quote copied "
                "from the text that fetch returned"
            )

        conflicts = citation.get("conflicts", None)

        if conflicts is not None and not isinstance(conflicts, list):
            return f"citation {marker} conflicts must be a list"

        if isinstance(conflicts, list):
            for item in conflicts:
                if not isinstance(item, dict):
                    return f"citation {marker} conflict must be an object"

                if (
                    not isinstance(item.get("attribute"), str)
                    or not item["attribute"].strip()
                ):
                    return f"citation {marker} conflict needs an attribute"

                values = item.get("values")

                if not isinstance(values, list) or not values:
                    return f"citation {marker} conflict needs a values list"

                for value in values:
                    if not isinstance(value, dict) or not isinstance(
                        value.get("value"), str
                    ):
                        return f"citation {marker} conflict value needs a value string"

                    if (
                        Contract._names_offsets(value)
                        and Contract._span_offsets(value) is None
                    ):
                        return (
                            f"citation {marker} conflict start and end must be offsets "
                            "with end greater than start"
                        )

        return None

    @staticmethod
    def _fetched_text(call: dict | None) -> str | None:
        """Note text stored for this call, or None when there is nothing to check."""
        if not isinstance(call, dict):
            return None

        span = call.get("span")

        if not isinstance(span, dict):
            return None

        text = span.get("text")
        return text if isinstance(text, str) and text else None

    @staticmethod
    def _quote_problem(
        marker: str, item: dict, text: str, call: dict | None
    ) -> str | None:
        """Refuse a quote that is not a passage in the fetched note."""
        quote = item.get("quote")

        if not isinstance(quote, str) or not quote.strip():
            return f"citation {marker} evidence needs a quote copied from the note"

        if len(text) > Contract.MAX_QUOTE and quote.strip() == text.strip():
            return (
                f"citation {marker} quotes the whole note; "
                "copy the passage that supports the claim"
            )

        found = Contract._find_quote(text, quote, Contract._quote_hint(item, call))

        if found is None:
            return (
                f"citation {marker} quote is not in the text that fetch returned; "
                "copy a contiguous passage from that text"
            )

        return None

    @staticmethod
    def _quote_hint(item: dict, call: dict | None) -> int | None:
        """Model offset, shifted into the fetched text when a quote occurs twice."""
        hint = Contract.as_int(item.get("start", item.get("char_start")))

        if hint is None or not isinstance(call, dict):
            return hint

        span = call.get("span")
        origin = Contract.as_int(span.get("origin")) if isinstance(span, dict) else None

        if origin:
            return hint - origin

        return hint

    @staticmethod
    def _find_quote(text: str, quote: str, hint: int | None) -> tuple[int, int] | None:
        """Offsets of this passage in the fetched note, or None when it is absent."""
        needle = quote.strip()

        if not needle:
            return None

        starts = []
        cursor = 0

        while True:
            found = text.find(needle, cursor)

            if found < 0:
                break

            starts.append(found)
            cursor = found + 1

        if starts:
            start = (
                starts[0]
                if hint is None
                else min(starts, key=lambda item: abs(item - hint))
            )
            return start, start + len(needle)

        return Contract._find_quote_loose(text, needle)

    @staticmethod
    def _find_quote_loose(text: str, needle: str) -> tuple[int, int] | None:
        """Find a passage whose words match, allowing different spacing."""
        flat_chars: list[str] = []
        index_map: list[int] = []
        previous_space = False

        for index, char in enumerate(text):
            if char.isspace():
                if flat_chars and not previous_space:
                    flat_chars.append(" ")
                    index_map.append(index)
                previous_space = True
                continue

            flat_chars.append(char)
            index_map.append(index)
            previous_space = False

        flat_needle = " ".join(needle.split())
        at = "".join(flat_chars).find(flat_needle)

        if at < 0 or not index_map:
            return None

        start = index_map[at]
        end = index_map[at + len(flat_needle) - 1] + 1
        return start, end

    # -----------------------------------------------------------------------
    # Offsets
    # -----------------------------------------------------------------------

    @staticmethod
    def _names_offsets(item: dict) -> bool:
        return any(key in item for key in ("start", "end", "char_start", "char_end"))

    @staticmethod
    def _span_offsets(item: dict) -> tuple[int, int] | None:
        """Character offsets on a quote, or None when they are absent or invalid."""
        start = Contract.as_int(item.get("start", item.get("char_start")))
        end = Contract.as_int(item.get("end", item.get("char_end")))

        if start is None and end is None:
            return None

        if start is None or end is None or end <= start or start < 0:
            return None

        return start, end

    @staticmethod
    def as_int(value: object) -> int | None:
        """An int, or None when the value is not a whole number."""
        if isinstance(value, bool) or value is None:
            return None

        if isinstance(value, int):
            return value

        if isinstance(value, float) and value.is_integer():
            return int(value)

        return None

    # -----------------------------------------------------------------------
    # Fields the server fills
    # -----------------------------------------------------------------------

    @staticmethod
    def apply(citation: dict, call: dict) -> None:
        """Fill id, file, web_path, command, and note links from one cite token."""
        citation["id"] = citation.get("cite")
        citation["file"] = call["file"]
        citation["web_path"] = call["web_path"]
        citation["command"] = {"tool": call["tool"], "arguments": call["arguments"]}

        if call.get("dicom"):
            citation["dicom"] = call["dicom"]

        Contract._fill_table(citation, call)
        Contract._fill_notes(citation, call)

    @staticmethod
    def _fill_table(citation: dict, call: dict) -> None:
        """Attach what the table call returned, and the checked value of each cell."""
        raw = call.get("table")

        if not isinstance(raw, dict) or not raw:
            return

        cells = []

        for cell in citation.get("cells") or []:
            if not isinstance(cell, dict):
                continue

            checked, _ = Contract._cell(raw, cell)

            if checked:
                cells.append(checked)

        if cells:
            citation["cells"] = cells
        else:
            citation.pop("cells", None)

        if raw.get("view") == "rows":
            # Only the cited rows travel with the citation; the page loads the
            # rest of the table from the file.
            kept = {str(cell["row"]) for cell in cells}
            rows = raw.get("rows") or {}
            citation["table"] = {
                "view": "rows",
                "columns": raw.get("columns") or [],
                "rows": {number: rows[number] for number in rows if number in kept},
            }
        else:
            citation["table"] = raw

    @staticmethod
    def basis(citation: dict, call: dict | None, by: str) -> dict:
        """What a citation rests on, and who checked it.

        ``type`` is ``quote`` (a passage of a note), ``cell`` (table cells),
        ``computed`` (a value code worked out), ``model_read`` (a model's
        description of an image), ``metadata``, or ``file``. ``checked`` is
        ``server`` when this server compared the claim's evidence with the file,
        ``specialist`` when only the specialist did, and ``none`` otherwise.
        """
        raw = citation.get("dicom")
        dicom = raw if isinstance(raw, dict) else {}
        has_quotes = any(
            isinstance(item, dict) and item.get("quote")
            for item in citation.get("evidence") or []
        )

        if citation.get("kind") == "derived":
            recorded = Contract.returned_by(call)
            kind, checked = "computed", "server" if recorded else "none"
        elif has_quotes:
            located = isinstance(call, dict) and isinstance(call.get("span"), dict)
            kind = "quote"
            checked = "server" if located else "none"

            if not located and by != "router":
                checked = "specialist"
        elif citation.get("cells"):
            kind, checked = "cell", "server"
        elif dicom.get("previews"):
            kind, checked = "model_read", "none"
        elif dicom.get("metadata"):
            kind, checked = "metadata", "server"
        else:
            kind, checked = "file", "none"

        return {"type": kind, "checked": checked, "by": by}

    @staticmethod
    def _fill_notes(citation: dict, call: dict) -> None:
        """Copy the note label onto each quote and fill span_url."""
        evidence = citation.get("evidence")
        conflicts = citation.get("conflicts")
        span = call.get("span") if isinstance(call.get("span"), dict) else None
        has_notes = isinstance(evidence, list) or isinstance(conflicts, list)

        if not has_notes:
            return

        arguments = call.get("arguments")
        resource_id = (
            arguments.get("resource_id") if isinstance(arguments, dict) else None
        )
        resource_id = resource_id if isinstance(resource_id, str) else ""
        patient_raw = call.get("patient_id")
        patient_id = patient_raw if isinstance(patient_raw, str) else ""

        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue

                if not item.get("label") and call.get("file"):
                    item["label"] = call["file"]

                if not item.get("note_id") and resource_id:
                    item["note_id"] = resource_id

                Contract._place_quote(item, span)
                note_id = item.get("note_id")
                doc_id = note_id if isinstance(note_id, str) else resource_id
                Contract._fill_span_url(item, patient_id, doc_id)

        if isinstance(conflicts, list):
            for conflict in conflicts:
                if not isinstance(conflict, dict):
                    continue

                values = conflict.get("values")

                if not isinstance(values, list):
                    continue

                for value in values:
                    if not isinstance(value, dict):
                        continue

                    note_id = value.get("note_id")
                    doc_id = note_id if isinstance(note_id, str) else resource_id
                    Contract._fill_span_url(value, patient_id, doc_id)

        if call.get("modality") == "note" and not citation.get("agent"):
            citation["agent"] = "clinical_notes"

    @staticmethod
    def _place_quote(item: dict, span: dict | None) -> None:
        """Turn fetched-text offsets into file offsets and copy the note label."""
        if not isinstance(span, dict):
            return

        raw_text = span.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
        origin = Contract.as_int(span.get("origin")) or 0
        quote = item.get("quote")
        found = None

        if isinstance(quote, str):
            found = Contract._find_quote(
                text, quote, Contract._quote_hint(item, {"span": span})
            )

        if found:
            local_start, local_end = found
            item["quote"] = text[local_start:local_end]
            item["start"] = origin + local_start
            item["end"] = origin + local_end

        if not item.get("note_date") and isinstance(span.get("note_date"), str):
            item["note_date"] = span["note_date"]

        if not item.get("section") and isinstance(span.get("section"), str):
            item["section"] = span["section"]

    @staticmethod
    def _fill_span_url(item: dict, patient_id: str, doc_id: str) -> None:
        """Point a quote at the span route when it names offsets and has no link yet."""
        if item.get("span_url") or not patient_id or not doc_id:
            return

        offsets = Contract._span_offsets(item)

        if offsets is None:
            return

        start, end = offsets
        item["span_url"] = note_span_url(patient_id, doc_id, start, end)
