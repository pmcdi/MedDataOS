# Testing

```bash
pixi run test        # every test: no model, no network
pixi run qc          # ruff format, ruff check, mypy
pixi run docs-build  # build these docs, failing on a broken link
```

## What the tests cover

`tests/` mirrors the package:

| Folder | Covers |
|---|---|
| `tests/core/` | The loop, model clients and retries, the evidence registry, the layering rules (`test_layers.py`) |
| `tests/core/datactrl/` | Discovery, tables, notes, DICOM, provenance |
| `tests/harness/` | Tools, skills, agents, the sandbox command, the outbound screen, MCP servers |
| `tests/server/` | The server end to end: the page's routes, citations, harness mode, agents, MCP calls |
| `tests/evals/` | Eval scoring |

Nothing calls a real model. Server tests start the real server on a small temporary case, with a fake OpenAI endpoint that plays the model from a script. MCP tests start a fake stdio server (`tests/harness/fixtures/fake_mcp_server.py`). Each test server gets an empty harness config, empty MCP registrations and its own cache, so your `configs/harness.toml`, `configs/mcp.json`, `~/.meddataos` and `.cache` never leak in.

## Opt-in end-to-end test

```bash
HARNESS_SANDBOX_RUNTIME=runc MEDDATAOS_TOTALSEG_E2E=1 pixi run pytest tests/server/test_totalseg_e2e.py
```

Runs TotalSegmentator in its container on the real RADCURE-0006 case through the server, with a scripted model, and checks the cited result passes the verifier. It needs the case (`MEDDATAOS_RADCURE_CASE`, default `../med-datactrl/data/rawdata/RADCURE-0006`), Docker, the `totalseg` environment and the weights, and takes about two minutes.

## Evals

The evals call the real model and cost tokens, so they are not part of `pixi run test`. See [Run the evals](../guides/evals.md).
