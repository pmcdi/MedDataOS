"""The open case: the patient card, the tools an agent may run, and its reads.

The citation checks are ``meddataos.core.verifier``; this records the calls
they check against.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import ClassVar

from meddataos.core.datactrl.tools import TOOL_DEFINITIONS, dispatch
from meddataos.core.verifier import Contract
from meddataos.core.verifier.paths import path_key, web_path
from meddataos.core.workspace import workspace_for
from meddataos.server.state import log, state

# ---------------------------------------------------------------------------
# Patient card
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    """Demographics the agent read out of the files. Empty until that reply arrives."""

    name: str = ""
    age: str = ""
    sex: str = ""
    blood_type: str | None = None
    allergies: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    data_dates: dict[str, str] = field(default_factory=dict)

    _DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

    @classmethod
    def from_agent(cls, data: object) -> Profile:
        """Keep only well-formed fields, so the page never renders unexpected types."""
        if not isinstance(data, dict):
            raise TypeError("the card is not a JSON object")

        return cls(
            name=cls._text(data, "name"),
            age=cls._text(data, "age"),
            sex=cls._text(data, "sex"),
            blood_type=cls._text(data, "blood_type") or None,
            allergies=cls._strings(data, "allergies"),
            conditions=cls._strings(data, "conditions"),
            data_dates=cls._dates(data.get("data_dates")),
        )

    def as_dict(self) -> dict:
        """The card as a plain dict the page can render."""
        return asdict(self)

    @staticmethod
    def _text(data: dict, key: str) -> str:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return ""
        return str(value).strip()

    @staticmethod
    def _strings(data: dict, key: str) -> list[str]:
        items = data.get(key)
        items = items if isinstance(items, list) else []
        return [
            item.strip() for item in items if isinstance(item, str) and item.strip()
        ]

    @staticmethod
    def _dates(value: object) -> dict[str, str]:
        dates = value if isinstance(value, dict) else {}
        return {
            str(folder): date
            for folder, date in dates.items()
            if isinstance(date, str) and Profile._DATE.fullmatch(date)
        }


@dataclass
class Patient:
    """The card the sidebar renders."""

    id: str
    name: str
    age: str = ""
    sex: str = ""
    blood_type: str | None = None
    allergies: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    modalities: list[dict] = field(default_factory=list)
    data_dates: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """The sidebar card as a plain dict."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Open case
# ---------------------------------------------------------------------------


def _run_label(tool: str) -> str:
    """How a derived call is named on the page: "exec run", "segment on totalseg"."""
    if tool.startswith("mcp__") and tool.count("__") >= 2:
        _, server, name = tool.split("__", 2)
        return f"{name} on {server}"

    return f"{tool} run"


class Case:
    """The folder this server opened, and the tools that read it."""

    # Set by the server, never by the agent: an agent's value is dropped.
    _SERVER_ARGS: ClassVar[tuple[str, ...]] = ("root", "case_id", "out_dir")
    _READS: ClassVar[set[str]] = {"describe", "fetch"}
    _MODALITIES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("notes", "Notes"),
        ("structured", "Tables"),
        ("dicom", "DICOM"),
    )

    def __init__(self) -> None:
        """Expose the datactrl tools the agent may call."""
        # Specialists are consulted at the same time, and each tool call runs on
        # a worker thread. One call at a time reads the case and takes a token.
        self._lock = threading.Lock()
        self.tools = [self._exposed(tool) for tool in TOOL_DEFINITIONS]
        self.tool_names = [tool["function"]["name"] for tool in self.tools]
        self._takes_root = {
            tool["function"]["name"]
            for tool in TOOL_DEFINITIONS
            if "root" in tool["function"]["parameters"].get("properties", {})
        }

    # -----------------------------------------------------------------------
    # Inventory
    # -----------------------------------------------------------------------

    def files(self) -> list[dict]:
        """Files the agent may cite.

        Reads the inventory lists discover actually returns.
        """
        return [
            {
                "id": ref.get("id"),
                "modality": ref.get("modality"),
                "label": ref.get("label"),
                "web_path": web_path(ref.get("path", "")),
            }
            for ref in self._rows()
        ]

    def identifiers(self) -> list[str]:
        """What names this patient: the case folder, file patient ids, the name.

        The outbound screen refuses a reference URL holding any of them.
        """
        found = [state.root.name] if state.root is not None else []
        found += [
            str(ref["patient_id"]) for ref in self._rows() if ref.get("patient_id")
        ]

        if state.profile.get("name"):
            found.append(str(state.profile["name"]))

        return list(dict.fromkeys(found))

    def dicom_preview_paths(self) -> list[str]:
        """Case paths whose citation should show a DICOM preview image."""
        return [path for path, _resource_id in self._dicom_previews()]

    def dicom_preview_id(self, cited_path: str) -> str | None:
        """Resource id for a cited path that can render a preview, or None."""
        for path, resource_id in self._dicom_previews():
            if path == cited_path:
                return resource_id
        return None

    def _dicom_previews(self) -> list[tuple[str, str]]:
        from meddataos.core.datactrl.utils.dicom_views import preview_supported

        found = []

        for ref in self._rows():
            if ref.get("modality") != "dicom" or not isinstance(ref.get("id"), str):
                continue

            raw_meta = ref.get("meta")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            check = {
                "series_kind": ref.get("series_kind") or meta.get("series_kind"),
                "Modality": ref.get("dicom_modality") or meta.get("Modality"),
            }

            if not preview_supported(check):
                continue

            found.append((web_path(ref.get("path", "")), ref["id"]))

        return found

    def _file_by_id(self, resource_id: object) -> dict | None:
        if not isinstance(resource_id, str) or not resource_id:
            return None

        files = self.files()
        exact = next((item for item in files if item.get("id") == resource_id), None)

        if exact is not None:
            return exact

        return next(
            (
                item
                for item in files
                if isinstance(item.get("id"), str)
                and len(resource_id) >= 8
                and len(item["id"]) >= 8
                and (
                    item["id"].endswith(resource_id) or resource_id.endswith(item["id"])
                )
            ),
            None,
        )

    def _rows(self) -> list[dict]:
        if state.report is None:
            return []

        rows = state.report.get("resources")

        if not isinstance(rows, list):
            rows = []

            for key in ("notes", "structured", "dicom"):
                found = state.report.get(key) or []

                if isinstance(found, list):
                    rows.extend(found)

        return [ref for ref in rows if isinstance(ref, dict)]

    def patient(self) -> Patient | None:
        """The card. Demographics come from the agent once it has read the files."""
        if state.root is None:
            return None

        info = state.profile
        report = state.report or {}

        return Patient(
            id=state.root.name,
            name=info.get("name") or state.root.name,
            age=info.get("age") or "",
            sex=info.get("sex") or "",
            blood_type=info.get("blood_type"),
            allergies=info.get("allergies") or [],
            conditions=info.get("conditions") or [],
            modalities=[
                {"label": label, "date": None}
                for key, label in self._MODALITIES
                if report.get(key)
            ],
            data_dates=info.get("data_dates") or {},
        )

    # -----------------------------------------------------------------------
    # Tools
    # -----------------------------------------------------------------------

    def run(
        self, name: str, arguments: dict, session_id: str, by: str = "router"
    ) -> dict:
        """Run one tool for a question and remember any file it opened.

        ``by`` is who asked: ``router``, or the name of a specialist.
        """
        with self._lock:
            return self._run(name, arguments, session_id, by)

    def _run(self, name: str, arguments: dict, session_id: str, by: str) -> dict:
        agent_args = {
            key: value
            for key, value in arguments.items()
            if key not in self._SERVER_ARGS
        }
        detail = " ".join(
            f"{key}={value}" for key, value in agent_args.items() if value is not None
        )
        args = dict(agent_args)

        if name in self._takes_root:
            args["root"] = str(state.root)

        # The harness's own model reads volumes as workspace files, so datactrl
        # writes them there directly: no base64 round trip, and the larger file
        # limit. An external agent at --agent-url still gets them inline.
        if state.harness and name == "fetch":
            args["out_dir"] = str(workspace_for(session_id or "card") / "volumes")

        try:
            result = json.loads(dispatch(name, args))
        except Exception as exc:  # noqa: BLE001 — return the tool failure to the agent
            message = str(exc) or exc.__class__.__name__
            log("agent", f"tool {name} {detail} error: {message}")
            return {"status": "error", "name": name, "message": message}

        payload = {"status": "ok", "name": name, "result": result}

        if name in self._READS:
            token = self._remember(session_id, name, agent_args, result, by)

            if token:
                payload["cite"] = token

        log("agent", f"tool {name} {detail} ok")
        return payload

    # -----------------------------------------------------------------------
    # Delivered citations
    # -----------------------------------------------------------------------

    def expand_citations(self, text: str, calls: dict[str, dict]) -> str:
        """Fill file, web_path, command, DICOM, and note links from each cite token.

        A specialist's finding was filled in when it was accepted and is left alone.
        """
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text

        citations = parsed.get("citations") if isinstance(parsed, dict) else None

        if not isinstance(citations, list) or not calls:
            return text

        changed = False

        for citation in citations:
            if not isinstance(citation, dict):
                continue

            if citation.get("finding"):
                continue

            handle = citation.get("cite")
            call = calls.get(handle) if isinstance(handle, str) else None

            if not isinstance(call, dict):
                continue

            Contract.apply(citation, call)
            changed = True

        if not changed:
            return text

        return json.dumps(parsed)

    def stamp_basis(self, text: str, calls: dict[str, dict]) -> str:
        """Say what each of the router's own citations rests on.

        A finding already says so: it was stamped when its specialist answered.
        """
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text

        citations = parsed.get("citations") if isinstance(parsed, dict) else None

        if not isinstance(citations, list):
            return text

        for citation in citations:
            if isinstance(citation, dict) and not citation.get("finding"):
                handle = citation.get("cite")
                call = calls.get(handle) if isinstance(handle, str) else None
                citation["basis"] = Contract.basis(citation, call, "router")

        return json.dumps(parsed)

    _AGENT_BY_MODALITY: ClassVar[dict[str, str]] = {
        "note": "clinical_notes",
        "dicom": "imaging",
        "structured": "structured",
    }

    def name_sources(self, text: str, calls: dict[str, dict]) -> str:
        """Name a citation from its file when the router left the agent empty.

        A model that must fill every field often sends ``agent: null``.
        A citation that already carries a DICOM record is left alone: the page
        names those itself.
        """
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text

        citations = parsed.get("citations") if isinstance(parsed, dict) else None

        if not isinstance(citations, list):
            return text

        modality_of = {
            path_key(item["web_path"]): item.get("modality") for item in self.files()
        }
        changed = False

        for citation in citations:
            if (
                not isinstance(citation, dict)
                or citation.get("agent")
                or citation.get("dicom")
            ):
                continue

            handle = citation.get("cite")
            call = calls.get(handle) if isinstance(handle, str) else None
            modality = (call or {}).get("modality") or modality_of.get(
                path_key(citation.get("web_path"))
            )
            agent = self._AGENT_BY_MODALITY.get(modality or "")

            if agent:
                citation["agent"] = agent
                changed = True

        return json.dumps(parsed) if changed else text

    # -----------------------------------------------------------------------
    # Calls remembered for the open question
    # -----------------------------------------------------------------------

    def _remember(
        self,
        session_id: str,
        tool: str,
        agent_args: dict,
        result: dict,
        by: str = "router",
    ) -> str | None:
        """Remember a read and return its cite token, or None."""
        if session_id not in state.pending:
            return None

        ref = self._file_by_id(agent_args.get("resource_id"))

        if ref is None and isinstance(result, dict):
            ref = self._file_by_id(result.get("id"))

        if ref is None:
            return None

        # Only the router's own reads count as files it opened. What a specialist
        # read reaches the router as a finding.
        if by == "router":
            opened = state.reads.setdefault(session_id, [])

            if not any(item["web_path"] == ref["web_path"] for item in opened):
                opened.append(ref)

        calls = state.calls.setdefault(session_id, {})
        token = f"c{len(calls) + 1}"
        record = {
            "tool": tool,
            "arguments": {
                key: value for key, value in agent_args.items() if value is not None
            },
            "file": path_key(ref.get("web_path")).rsplit("/", 1)[-1],
            "web_path": ref["web_path"],
            "modality": ref.get("modality") or "",
            "patient_id": state.root.name if state.root is not None else "",
            "by": by,
        }
        table = self._call_table(ref, tool, result)

        if table:
            record["table"] = table

        dicom = self._call_dicom(ref, tool, agent_args, result)

        if dicom:
            record["dicom"] = dicom

        span = self._call_span(ref, tool, agent_args, result)

        if span:
            record["span"] = span

        calls[token] = record
        return token

    def remember_output(
        self,
        session_id: str,
        tool: str,
        agent_args: dict,
        output: dict,
        by: str = "router",
    ) -> str | None:
        """Record a derived call, such as an exec run, and return its cite token.

        A derived call reads no case file: what it printed is the record the
        verifier checks a cited value against.
        """
        if session_id not in state.pending:
            return None

        calls = state.calls.setdefault(session_id, {})
        token = f"c{len(calls) + 1}"
        calls[token] = {
            "tool": tool,
            "arguments": {
                key: value for key, value in agent_args.items() if value is not None
            },
            "file": _run_label(tool),
            "web_path": "",
            "modality": "derived",
            "patient_id": state.root.name if state.root is not None else "",
            "by": by,
            "output": output,
        }
        return token

    def _call_dicom(self, ref: dict, tool: str, args: dict, result: dict) -> dict:
        """Metadata, or the preview slice the page shows for this call."""
        if ref.get("modality") != "dicom":
            return {}

        if tool == "describe":
            metadata = self._dicom_metadata(result.get("summary"))
            return {"metadata": metadata} if metadata else {}

        view = result.get("view")

        if view not in ("metadata", "preview"):
            view = args.get("view")

        if view == "metadata":
            data = result.get("data")
            raw = data.get("metadata") if isinstance(data, dict) else None
            metadata = self._dicom_metadata(raw)
            return {"metadata": metadata} if metadata else {}

        if view == "preview":
            preview = self._dicom_preview(args, result, ref.get("web_path", ""))
            return {"previews": [preview]} if preview else {}

        if view == "volume":
            return {"previews": [self._volume_preview(ref, args)]}

        return {}

    @staticmethod
    def _volume_preview(ref: dict, args: dict) -> dict:
        """A slice of the series a volume came from, for the page to show.

        A volume is a file, not a picture, so a cited volume would otherwise show
        nothing. The page renders this slice on demand: a structure set with its
        contours drawn (the named ROI when one was asked for), a scan on its own.
        """
        preview: dict[str, object] = {"web_path": ref.get("web_path", "")}

        if ref.get("series_kind") in ("rtstruct", "seg"):
            preview["overlay"] = True

        roi = args.get("roi")

        if isinstance(roi, str) and roi.strip():
            preview["roi"] = roi.strip()
            preview["overlay"] = True

        return preview

    _SERIES_KEYS: ClassVar[tuple[str, ...]] = (
        "item",
        "key",
        "unit",
        "value_column",
        "time_column",
        "points",
        "stats",
        "text_values",
        "other_units",
        "superseded",
    )

    @classmethod
    def _call_table(cls, ref: dict, tool: str, result: dict) -> dict:
        """What a table fetch returned, so a citation of it can be checked.

        The page draws the chart and the cited cells from this record, not from
        what the agent wrote.
        """
        if ref.get("modality") != "structured" or tool != "fetch":
            return {}

        view = result.get("view")
        raw = result.get("data")
        data = raw if isinstance(raw, dict) else {}

        if view == "rows":
            numbers = data.get("row_numbers") or []
            rows = data.get("rows") or []
            return {
                "view": "rows",
                "columns": list(data.get("columns") or []),
                "rows": {
                    str(number): row
                    for number, row in zip(numbers, rows, strict=False)
                    if isinstance(row, dict)
                },
            }

        if view == "series":
            return {
                "view": "series",
                **{key: data[key] for key in cls._SERIES_KEYS if key in data},
            }

        if view == "score":
            return {"view": "score", **data}

        return {}

    @staticmethod
    def _dicom_metadata(raw: object) -> dict:
        """The metadata object the tool returned, without empty fields."""
        if not isinstance(raw, dict):
            return {}

        return {
            key: value
            for key, value in raw.items()
            if value is not None and value != "" and value != []
        }

    @staticmethod
    def _dicom_preview(args: dict, result: dict, cited_path: str) -> dict:
        raw_data = result.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        raw_bounds = result.get("bounds")
        bounds = raw_bounds if isinstance(raw_bounds, dict) else {}
        raw_meta = data.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        preview: dict[str, object] = {}

        if cited_path:
            preview["web_path"] = cited_path

        slice_index = bounds.get("slice_index", args.get("slice_index"))

        if isinstance(slice_index, bool):
            slice_index = None
        elif isinstance(slice_index, float) and slice_index.is_integer():
            slice_index = int(slice_index)
        elif isinstance(slice_index, str) and slice_index.strip().isdigit():
            slice_index = int(slice_index.strip())

        if isinstance(slice_index, int):
            preview["slice_index"] = slice_index

        n_slices = bounds.get("n_slices")

        if isinstance(n_slices, int):
            preview["n_slices"] = n_slices

        window = bounds.get("window") or meta.get("window")

        if isinstance(window, str) and window:
            preview["window"] = window

        if meta.get("overlay") is True or args.get("overlay") in (
            True,
            "true",
            "True",
            1,
        ):
            preview["overlay"] = True

        roi = args.get("roi")

        if isinstance(roi, str) and roi.strip():
            preview["roi"] = roi.strip()

        return preview

    @staticmethod
    def _call_span(ref: dict, tool: str, args: dict, result: dict) -> dict:
        """The note text a fetch returned, so a citation can be checked against it.

        Offsets on the citation are into this text. ``origin`` is the file offset of
        ``text[0]``: 0 for a text fetch, and the span start for a span fetch.

        """
        if ref.get("modality") != "note" or tool != "fetch":
            return {}

        view = result.get("view") or args.get("view")
        raw_data = result.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        raw_text = data.get("text")
        text = raw_text if isinstance(raw_text, str) else ""

        if view not in ("text", "span") or not text.strip():
            return {}

        if view == "text":
            start, end, origin = 0, len(text), 0
        else:
            start_at = Contract.as_int(data.get("start", args.get("start")))
            end_at = Contract.as_int(data.get("end", args.get("end")))

            if start_at is None or end_at is None or end_at <= start_at:
                return {}

            start, end, origin = start_at, end_at, start_at

        span: dict[str, object] = {
            "start": start,
            "end": end,
            "text": text,
            "origin": origin,
        }
        section = data.get("section")

        if isinstance(section, str) and section.strip():
            span["section"] = section.strip()

        meta_raw = ref.get("meta")
        meta = meta_raw if isinstance(meta_raw, dict) else {}
        note_date = meta.get("note_date")

        if isinstance(note_date, str) and note_date.strip():
            span["note_date"] = note_date.strip()

        return span

    # -----------------------------------------------------------------------
    # Tool list shown to the agent
    # -----------------------------------------------------------------------

    @staticmethod
    def _exposed(tool: dict) -> dict:
        """The datactrl tool, without the arguments the server fills in."""
        spec = copy.deepcopy(tool)
        params = spec["function"]["parameters"]

        for key in Case._SERVER_ARGS:
            params.get("properties", {}).pop(key, None)

        params["required"] = [
            key for key in params.get("required", []) if key not in Case._SERVER_ARGS
        ]
        return spec


case = Case()
