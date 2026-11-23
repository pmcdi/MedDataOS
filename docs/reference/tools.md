# Tools

The tools the model can call, when each is offered, and how the verifier treats what it returns. A tool that cannot run is not offered.

## Case reads

These go through the controller (datactrl), the only path to the files. Their full parameters are in the [datactrl docs](../datactrl/README.md).

| Tool | What it does | Evidence |
|---|---|---|
| `describe` | A resource's metadata, views and links | Case: citable |
| `fetch` | A bounded view: note text or a span, table rows, a series, a score, DICOM metadata, a preview slice, a volume | Case: citable |
| `discover` | The case inventory | Navigation |
| `notes_search` | Search the notes (BM25), with character offsets | Navigation |
| `links`, `assert_link` | Links between resources; record one for this session | Navigation |
| `dicom_tree`, `dicom_groups` | DICOM studies and series, and how they reference each other | Navigation |

In harness mode, a volume `fetch` writes the NIfTI into the session workspace and returns its path, up to 512 × 512 × 1024 voxels. An external agent gets it inline as base64, up to 33.5 M voxels.

## Harness tools

| Tool | What it does | Offered when | Evidence |
|---|---|---|---|
| `exec` | Run a shell command in the sandbox | The sandbox can run | Derived |
| `view_image` | Look at a PNG or JPEG in the workspace | The sandbox can run | Navigation |
| `reference_search` | Search the allowlisted reference sites | Always | Background |
| `reference_read` | Read one reference page | Always | Background |
| `ledger_query` | Facts from the notes, each with its note and offsets | The case has notes and the ledger is on | Navigation |
| `notes_timeline` | Dated events from the notes | Same | Navigation |
| `notes_conflicts` | Where notes disagree | Same | Navigation |
| `load_skill` | A skill's instructions | A skill exists | Navigation |
| `spawn_subagent` | Run an agent in its own context | An agent is enabled | Control |
| `mcp__<server>__<tool>` | A tool from an MCP server | The server connected, and the caller was granted it (the router, when `router` is set; an agent, in its file) | The server's `evidence` |

## Evidence classes

Each tool is registered in `core/registry.py` with a class. The system prompt's citing rule is generated from the same registry, so the model is told what the verifier accepts.

| Class | Treated as |
|---|---|
| Case | Citable by cite token; checked against the recorded read |
| Derived | Citable as a derived value; a cited number must be one the run returned |
| Background | Never evidence about the patient; named in prose without a marker |
| Navigation | Not citable; leads to a read that is |
| Control | Returns a subagent's checked findings, cited by finding id such as `dicom.1` |

A tool that is not registered is background, so a new tool cannot become evidence by accident.

## Results the model sees

Every result is JSON: `{"status": "ok", "name": ..., "result": ...}`, plus `"cite": "c3"` for a citable read or run. A failure is `{"status": "error", "name": ..., "message": ...}`, and the model can try again; a failed call does not stop the question.

Binary data never enters the model's context. Previews, PDF pages and volumes are saved in the session workspace and replaced by their `workspace_path`. A picture the model asks to see (`view_image`, or an image an MCP tool returns) is sent as an image message after the tool result.
