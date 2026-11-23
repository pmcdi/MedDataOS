# Use an external agent

Instead of driving the model itself, the server can hand each question to an agent you run: any HTTP server that accepts OpenAI chat completions. The server still offers the case tools, runs them, and checks the answer's citations, so your agent gets the same guarantees. Use this to try your own agent loop or a framework against MedDataOS.

## Run the example agent

`examples/agent.py` forwards each request to your model unchanged, which makes it a good starting point:

```bash
pixi run agent        # listens on http://127.0.0.1:8001/v1
```

In another terminal:

```bash
pixi run serve /path/to/folder --agent-url http://127.0.0.1:8001/v1
```

The example agent uses the model in `.env` (`OPENAI_*`, or the Azure settings). It exits if neither is set.

## What changes in this mode

- The agent gets only the case tools (`discover`, `describe`, `fetch`, note search, DICOM trees and groups). Skills, subagents, the sandbox, references, the ledger and MCP servers belong to the harness and are not offered.
- Volumes come back inline as base64, so a volume is limited to 33.5 M voxels; pass a coarser `spacing` for a large scan.
- The server sends `AGENT_MODEL` as the model name, `AGENT_API_KEY` as a bearer token when set, and waits `AGENT_TIMEOUT` seconds (default 120) for each response.

[Agent protocol](../reference/agent-protocol.md) specifies exactly what your agent receives and must return.
