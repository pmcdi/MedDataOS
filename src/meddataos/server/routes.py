"""Web routes: the page and its socket, chat, and the sidebar data."""

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from meddataos.core.datactrl.case import Case
from meddataos.core.datactrl.limits import MAX_TABLE_LIMIT
from meddataos.core.verifier.paths import path_key
from meddataos.server import dispatch, store, timeline
from meddataos.server.case import case
from meddataos.server.state import APP_ROOT, log, short_session, state

router = APIRouter()

FRONTEND = APP_ROOT / "web"


@router.get("/")
async def read_root():
    """The page."""
    return FileResponse(
        str(FRONTEND / "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


@router.websocket("/ws")
async def browser_socket(websocket: WebSocket):
    """Keep one page socket and send it the patient card when the card is ready."""
    await websocket.accept()
    state.hub.connect(websocket)
    log("page", f"connected ({len(state.hub.connections)} open)")
    reason = ""

    try:
        await websocket.send_json(
            {
                "type": "system",
                "message": "Connected to MedDataOS",
                "timestamp": "",
            }
        )

        card = case.patient()

        if state.profile and card:
            await websocket.send_json({"type": "patient", "patient": card.as_dict()})

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        reason = f": {exc}"
    finally:
        state.hub.disconnect(websocket)
        log("page", f"disconnected ({len(state.hub.connections)} open){reason}")


def _opened_case():
    """The datactrl case for span reads. Opened once and kept on the process."""
    if state.root is None:
        return None

    if state.datactrl_case is None:
        state.datactrl_case = Case.open(state.root)

    return state.datactrl_case


def _ledger_files(patient_id: str):
    """Places a patient's ledger might have been saved, nearest folders first."""
    filename = f"{patient_id}.json"
    configured = os.environ.get("NOTES_LEDGER_DIR")
    if configured:
        yield Path(configured) / filename
    if state.root is None:
        return
    stop = {Path.home(), Path(state.root.anchor)}
    ancestors: list[Path] = []
    for parent in [state.root, *state.root.parents]:
        if parent in stop or len(ancestors) >= 6:
            break
        ancestors.append(parent)
    for parent in ancestors:
        yield parent / "workspace" / "notes_ledger" / filename
        try:
            children = [child for child in parent.iterdir() if child.is_dir()]
        except OSError:
            continue
        for child in children:
            yield child / "workspace" / "notes_ledger" / filename


def _ocr_doc(patient_id: str, doc_id: str) -> dict | None:
    """OCR transcript stored in the notes ledger, when that file is on disk."""
    for path in _ledger_files(patient_id):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        doc = (data.get("docs") or {}).get(doc_id)
        if isinstance(doc, dict) and isinstance(doc.get("text"), str):
            return doc
    return None


def note_span_payload(
    patient_id: str, doc_id: str, start: int, end: int, context: int = 0
):
    """One note with highlight offsets, or an error dict and status code."""
    opened = _opened_case()
    if opened is None or state.root is None or state.root.name != patient_id:
        return 404, {"error": "that case is not open"}

    base_id = doc_id.split("#", 1)[0]
    ref = next((note for note in opened.report.notes if note.id == base_id), None)
    if ref is None:
        return 404, {"error": f"unknown note id {doc_id}"}

    meta = ref.meta if isinstance(ref.meta, dict) else {}
    label = ref.label
    derived = "native"
    page_index = None

    if "#" in doc_id:
        doc = _ocr_doc(patient_id, doc_id)
        if doc is None:
            return 404, {"error": f"no OCR transcript for {doc_id}"}
        text = doc["text"]
        label = doc.get("label") or label
        derived = doc.get("derived") or "ocr"
        page_index = doc.get("page_index")
    else:
        from meddataos.core.datactrl.limits import MAX_NOTE_CHARS

        try:
            fetched = opened.fetch(doc_id, view="text", max_chars=MAX_NOTE_CHARS)
            text = (fetched.get("data") or {}).get("text") or ""
        except Exception as exc:
            return 400, {"error": str(exc)}

    if not (0 <= start < end <= len(text)):
        return 400, {
            "error": f"span {start}-{end} outside document ({len(text)} chars)"
        }

    if context > 0:
        lo, hi = max(0, start - context), min(len(text), end + context)
    else:
        lo, hi = 0, len(text)

    return 200, {
        "note_id": doc_id,
        "label": label,
        "note_date": meta.get("note_date"),
        "note_type": meta.get("note_type"),
        "derived": derived,
        "page_index": page_index,
        "text": text[lo:hi],
        "offset": lo,
        "highlight": {"start": start - lo, "end": end - lo},
        "truncated_before": lo > 0,
        "truncated_after": hi < len(text),
    }


@router.get("/api/cases/{patient_id}/notes/span")
async def note_span(patient_id: str, id: str, start: int, end: int, context: int = 0):
    """Highlighted source note for a clinical-notes citation."""
    status, payload = note_span_payload(patient_id, id, start, end, context)
    if status != 200:
        return JSONResponse(status_code=status, content=payload)
    return payload


def dicom_preview_payload(
    cited_path: str,
    slice_index: int | None = None,
    window: str | None = None,
    overlay: bool = False,
    roi: str | None = None,
) -> tuple[int, bytes | dict]:
    """PNG bytes for one fetched DICOM slice, or an error dict and status code."""
    resource_id = case.dicom_preview_id(cited_path)
    opened = _opened_case()

    if resource_id is None or opened is None:
        return 404, {"error": "no DICOM preview for that file"}

    options: dict = {}

    if slice_index is not None:
        options["slice_index"] = slice_index

    if window:
        options["window"] = window

    if overlay or roi:
        options["overlay"] = True

    if roi:
        options["roi"] = roi

    try:
        fetched = opened.fetch(resource_id, view="preview", **options)
        encoded = (fetched.get("data") or {}).get("image_base64")
        png = base64.b64decode(encoded) if isinstance(encoded, str) else b""
    except Exception as exc:
        return 400, {"error": str(exc)}

    if not png:
        return 404, {"error": "preview had no image"}

    return 200, png


@router.get("/api/dicom/previews")
async def dicom_previews():
    """Paths the page should render as a DICOM preview when cited."""
    return case.dicom_preview_paths()


@router.get("/api/dicom/preview")
async def dicom_preview(
    web_path: str,
    slice_index: int | None = None,
    window: str | None = None,
    overlay: bool = False,
    roi: str | None = None,
):
    """The preview image for one cited DICOM slice."""
    status, payload = dicom_preview_payload(web_path, slice_index, window, overlay, roi)

    if status != 200:
        return JSONResponse(status_code=status, content=payload)

    return Response(content=payload, media_type="image/png")


def table_view_payload(cited_path: str, rows: str = "") -> tuple[int, dict]:
    """A table as the page shows it, with the cited rows marked.

    The file is parsed here, not by the page, so a quoted comma inside a cell
    stays inside its cell. A table longer than one page is shown around the
    first cited row.
    """
    opened = _opened_case()

    if opened is None:
        return 404, {"error": "no case is open"}

    wanted = path_key(cited_path)
    ref = next(
        (item for item in opened.report.structured if path_key(item.path) == wanted),
        None,
    )

    if ref is None:
        return 404, {"error": "no table at that path"}

    marked = sorted({int(part) for part in rows.split(",") if part.strip().isdigit()})

    try:
        summary = opened.describe(ref.id)["summary"]
        total = int(summary.get("row_count") or 0)
        offset = 0

        if total > MAX_TABLE_LIMIT and marked:
            offset = max(0, min(marked[0] - 50, total - MAX_TABLE_LIMIT))

        data = opened.fetch(ref.id, view="rows", limit=MAX_TABLE_LIMIT, offset=offset)[
            "data"
        ]
    except Exception as exc:
        return 400, {"error": str(exc)}

    columns = list(data.get("columns") or [])
    return 200, {
        "label": ref.label,
        "shape": summary.get("shape"),
        "columns": columns,
        "column_types": {
            name: (summary.get("column_types") or {}).get(name, {}).get("type")
            for name in columns
        },
        "rows": [
            ["" if row.get(name) is None else str(row.get(name)) for name in columns]
            for row in data.get("rows") or []
        ],
        "row_numbers": list(data.get("row_numbers") or []),
        "total": total,
        "highlight": marked,
    }


@router.get("/api/tables/view")
async def table_view(web_path: str, rows: str = ""):
    """The table behind a citation, with its cited rows marked."""
    status, payload = table_view_payload(web_path, rows)

    if status != 200:
        return JSONResponse(status_code=status, content=payload)

    return payload


@router.get("/api/patients/{patient_id}/timeline")
async def patient_timeline(patient_id: str):
    """Dated notes, studies, and table rows of the open case, in lanes."""
    opened = _opened_case()

    if opened is None or state.root is None or state.root.name != patient_id:
        return JSONResponse(status_code=404, content={"error": "that case is not open"})

    if state.timeline is None:
        state.timeline = timeline.build(opened)

    return state.timeline


@router.get("/api/health")
async def health():
    """Whether a case is open, how many pages and chats are connected, MCP status."""
    return {
        "status": "healthy",
        "case": str(state.root) if state.root else None,
        "active_connections": len(state.hub.connections),
        "active_sessions": len(state.sessions),
        "mcp": dispatch.mcp_status(),
    }


UPLOAD_TYPES = (".csv", ".xlsx")


async def _save_upload(file: UploadFile) -> Path:
    """Save an uploaded table to data/input. Raises ValueError for other file types."""
    name = (file.filename or "").lower()

    if not name.endswith(UPLOAD_TYPES):
        raise ValueError("Only CSV and Excel (.csv, .xlsx) files are allowed")

    dest = APP_ROOT / "data" / "input" / f"dataset{Path(name).suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        dest.write_bytes(await file.read())
    except OSError as exc:
        raise ValueError(f"Failed to save file: {exc}") from exc
    finally:
        await file.close()

    return dest


@router.post("/api/chat")
async def chat(
    message: str = Form(...),
    session_id: str | None = Form(None),
    patient_id: str | None = Form(None),
    file: UploadFile | None = File(None),
):
    """Start a question. An optional table upload is saved beside the message."""
    message = message.strip()

    if not message:
        return {"status": "error", "message": "Message is required"}

    if session_id not in state.sessions:
        session_id = state.new_session(patient_id)

    session = state.sessions[session_id]
    label = short_session(session_id)

    if patient_id and session.patient_id in (None, patient_id):
        session.patient_id = patient_id

    if session.processing:
        log("user", f"session={label} still waiting on the previous question")
        return {
            "status": "error",
            "message": "Session is currently processing a request. Please wait.",
            "session_id": session_id,
        }

    if file:
        try:
            session.uploaded_file = str(await _save_upload(file))
        except ValueError as exc:
            return {"status": "error", "message": str(exc), "session_id": session_id}

        message += f"\n[Uploaded file: {file.filename}]"

    session.processing = True
    session.add("user", message)
    state.save()
    log("user", f"session={label} patient={session.patient_id or '?'} {message}")

    dispatch.ask(session_id)
    return {"status": "processing", "session_id": session_id}


@router.get("/api/session/{session_id}")
async def get_session(session_id: str):
    """One chat: its messages and whether a question is still running."""
    session = state.sessions.get(session_id)

    if session is None:
        return {"status": "error", "message": "Session not found"}

    return {
        "status": "success",
        "session_id": session_id,
        "message_count": len(session.history),
        "processing": session.processing,
        "history": session.history,
        "steps": session.steps,
        "activity": session.status if session.processing else "",
        "elapsed_ms": session.elapsed_ms() if session.processing else None,
    }


@router.get("/api/session/{session_id}/audit")
async def session_audit(session_id: str, limit: int = 100):
    """Audit runs for one chat session, newest first."""
    if state.root is None:
        return {"status": "error", "message": "No case is open"}

    if session_id not in state.sessions:
        return {"status": "error", "message": "Session not found"}

    return {
        "status": "success",
        "session_id": session_id,
        "runs": store.list_audit_runs(state.root, session_id=session_id, limit=limit),
    }


@router.get("/api/patients/{patient_id}/audit")
async def patient_audit(patient_id: str, limit: int = 100):
    """Audit runs for one patient across sessions, newest first."""
    if state.root is None:
        return {"status": "error", "message": "No case is open"}

    return {
        "status": "success",
        "patient_id": patient_id,
        "runs": store.list_audit_runs(state.root, patient_id=patient_id, limit=limit),
    }


@router.get("/api/patients")
async def list_patients():
    """The open case, as the one patient the sidebar knows."""
    found = case.patient()
    return [found.as_dict()] if found else []


@router.get("/api/patients/{patient_id}/conversations")
async def list_conversations(patient_id: str):
    """Chats for this patient."""
    return state.conversations(patient_id)


@router.post("/api/patients/{patient_id}/conversations")
async def create_conversation(patient_id: str):
    """A new empty chat for this patient."""
    session_id = state.new_session(patient_id)
    return {"session_id": session_id, "patient_id": patient_id}


@router.get("/api/patients/{patient_id}/pins")
async def list_pins(patient_id: str):
    """Pins saved on this patient."""
    return state.pins.get(patient_id, [])


def _same_citation(pin: dict, citation: dict) -> bool:
    pinned = pin.get("citation") or {}
    return (
        pin.get("type", "citation") == "citation"
        and pinned.get("agent") == citation.get("agent")
        and pinned.get("web_path") == citation.get("web_path")
    )


def _same_text(pin: dict, text: str) -> bool:
    return pin.get("type") == "text" and pin.get("text") == text


@router.post("/api/patients/{patient_id}/pins")
async def add_pin(patient_id: str, request: dict):
    """Save a citation or a text pin, unless the same one is already saved."""
    pin_type = request.get("type", "citation")
    pins = state.pins.setdefault(patient_id, [])

    if pin_type == "citation":
        citation = request.get("citation")

        if not citation:
            return {"status": "error", "message": "citation is required"}

        duplicate = next((pin for pin in pins if _same_citation(pin, citation)), None)
        pin = {
            "type": "citation",
            "citation": citation,
            "created_at": datetime.now(UTC).isoformat(),
            "query": request.get("query", ""),
            "annotations": {"text": "", "tags": []},
        }

    elif pin_type == "text":
        text = request.get("text", "").strip()

        if not text:
            return {"status": "error", "message": "text is required"}

        duplicate = next((pin for pin in pins if _same_text(pin, text)), None)
        pin = {"type": "text", "text": text}

    else:
        return {"status": "error", "message": "invalid pin type"}

    if duplicate:
        return {"status": "duplicate", "pin_id": duplicate["pin_id"]}

    pin_id = str(uuid4())
    pins.append({"pin_id": pin_id, **pin, "source": request.get("source", "")})
    state.save()
    return {"status": "ok", "pin_id": pin_id}


@router.delete("/api/patients/{patient_id}/pins/{pin_id}")
async def remove_pin(patient_id: str, pin_id: str):
    """Drop one pin."""
    pins = state.pins.get(patient_id, [])
    state.pins[patient_id] = [pin for pin in pins if pin["pin_id"] != pin_id]
    state.save()
    return {"status": "ok"}


@router.patch("/api/patients/{patient_id}/pins/{pin_id}")
async def patch_pin(patient_id: str, pin_id: str, request: dict):
    """Replace the annotations on one pin."""
    pin = next(
        (pin for pin in state.pins.get(patient_id, []) if pin["pin_id"] == pin_id), None
    )

    if pin is None:
        return {"status": "error", "message": "Pin not found"}

    if "annotations" in request:
        pin["annotations"] = request["annotations"]
        state.save()

    return pin


@router.get("/api/patients/{patient_id}/reviews")
async def list_reviews(patient_id: str):
    """Checklist state saved for this patient."""
    return state.reviews.get(patient_id, {})


@router.patch("/api/patients/{patient_id}/reviews")
async def patch_review(patient_id: str, request: dict):
    """Save checklist state under one key."""
    key = request.get("key", "").strip()

    if not key:
        return JSONResponse(status_code=422, content={"error": "key is required"})

    checklist_state = request.get("checklist_state", {})
    state.reviews.setdefault(patient_id, {})[key] = checklist_state
    state.save()

    return {"key": key, "checklist_state": checklist_state}
