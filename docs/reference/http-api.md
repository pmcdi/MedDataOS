# HTTP API

The routes the page uses. They serve one open case, bound to `HOST:PORT` (default `127.0.0.1:8080`), and have no authentication: keep the server on localhost or behind your own access control.

## Questions

| Route | What it does |
|---|---|
| `POST /api/chat` | Ask a question. Body: `message`, and `session_id` and `patient_id` when continuing a conversation. Returns the `session_id`; the answer arrives over the WebSocket. A second question in a session that is still answering is refused. |
| `GET /api/session/{id}` | The conversation's history. While a question runs: its `steps`, current `activity` and `elapsed_ms`. |
| `WS /ws` | Live messages for open pages (below). |

### WebSocket messages

| Type | When |
|---|---|
| `step` | A tool (`kind: tool`) or subagent run (`kind: subagent`) started, finished or failed |
| `status` | What the question is waiting on between steps: `Thinking`, `Waiting for <name>`, `Checking citations`, `Revising the answer` |
| `assistant` | The delivered answer, with its `steps` and `duration_ms` |
| `patient` | The patient card, once the model has filled it in |

A step has `step_id`, `status` (`started`, `done` or `error`), a short `label`, `started_ms`, and once it ends `duration_ms` and often a one-line `detail`. A step a subagent made carries `by` (its name) and `parent` (the subagent run's step id). A subagent step carries the task it was sent and, once done, its reply without citation objects, the number of findings, and any `needs` and `abstentions`. A finished `describe` or `fetch` carries `cite` and `cite_kind` (`note`, `imaging`, `table` or `derived`). Every step and status carries `elapsed_ms`. Labels never include file contents or tool arguments.

The delivered answer is the citation JSON described in [Answer format](answer-format.md), or plain text when no file was read. Each assistant entry also records the tokens it took, per party (`router`, each subagent).

## The case

| Route | What it does |
|---|---|
| `GET /api/patients` | The open case, as the one patient |
| `GET /api/patients/{id}/timeline` | Dated files in lanes (notes, imaging, one per table), built from the files' own dates |
| `GET /case/<path>` | A file from the case folder. A citation's `web_path` points here. |
| `GET /api/cases/{id}/notes/span` | A note with one passage highlighted: `id`, `start`, `end` |
| `GET /api/tables/view` | A table parsed on the server, with cited rows marked: `web_path`, `rows` |
| `GET /api/dicom/preview`, `GET /api/dicom/previews` | DICOM preview slices for citations |
| `GET /api/health` | `status`, the open case, connected pages and sessions, and `mcp`: each MCP server's status |

## Conversations, pins and reviews

| Route | What it does |
|---|---|
| `GET`, `POST /api/patients/{id}/conversations` | List conversations; start an empty one |
| `GET`, `POST /api/patients/{id}/pins` | List pins; pin a citation or a text selection. Pinning the same citation or text again returns the existing pin. |
| `PATCH`, `DELETE /api/patients/{id}/pins/{pin_id}` | Set a pin's note and tags; remove it |
| `GET`, `PATCH /api/patients/{id}/reviews` | Review checklist state, keyed by citation path |

## Audit

| Route | What it does |
|---|---|
| `GET /api/session/{id}/audit` | Audit runs for one conversation, newest first |
| `GET /api/patients/{id}/audit` | Audit runs across the case's conversations |

Both accept `limit` (default 100, at most 500). [Storage](storage.md) describes what a run holds.
