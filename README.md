# MedDataOS

MedDataOS answers questions about one patient's files. Point it at a folder of clinical notes, lab tables and DICOM studies, open the page, and ask. Each answer cites the files it used, and the server checks every citation against what was actually read before you see it: a quote must be in the note, a lab value must be the cell it names, a number must be one the read returned.

Everything runs locally. The model can be Azure OpenAI, OpenAI, or a local OpenAI-compatible server.

## Quick start

You need [pixi](https://pixi.sh) and a model that takes function tools.

```bash
pixi install
cp .env.example .env        # then set AZURE_OPENAI_* or OPENAI_*
pixi run serve evals/cases/SYN-EVAL-01
```

Open the URL it prints and ask "What was the highest AFP and when was it drawn?". [Get started](docs/getting-started.md) walks through it, including what to expect.

## Documentation

| | |
|---|---|
| [Get started](docs/getting-started.md) | Install, connect a model, ask your first question |
| [Use the page](docs/using-the-page.md) | Asking, reading cited answers, the trace, pins, reviews, the audit export |
| [How it works](docs/concepts/how-it-works.md) and [Cited answers](docs/concepts/citations.md) | What happens to a question, and what makes an answer pass |
| Guides | [Connect a model](docs/guides/models.md), [the sandbox](docs/guides/sandbox.md), [TotalSegmentator](docs/guides/totalsegmentator.md), [MCP servers](docs/guides/mcp-servers.md), [skills](docs/guides/skills.md), [agents](docs/guides/agents.md), [an external agent](docs/guides/external-agent.md), [evals](docs/guides/evals.md), [troubleshooting](docs/guides/troubleshooting.md) |
| Reference | [Settings](docs/reference/settings.md), [harness config](docs/reference/harness-config.md), [tools](docs/reference/tools.md), [answer format](docs/reference/answer-format.md), [agent protocol](docs/reference/agent-protocol.md), [HTTP API](docs/reference/http-api.md), [storage](docs/reference/storage.md), [datactrl](docs/datactrl/README.md) |
| Development | [Code map](docs/development/architecture.md), [testing](docs/development/testing.md) |

`pixi run docs` serves the documentation locally; a push to `main` publishes it to GitHub Pages.

## Repository

| Path | Contents |
|---|---|
| `src/meddataos/` | The package: `core` (the controller `datactrl`, the model loop and clients, the evidence registry, the citation verifier), `harness` (tools, skills, agents, sandbox, MCP servers, notes ledger), `server` (HTTP and sessions) and `evals`. See the [code map](docs/development/architecture.md). |
| `web/` | The page. |
| `skills/` | Domain know-how, one `SKILL.md` per domain: `dicom`, `notes`, `tabular`. A skill grants no tools. |
| `agents/` | Subagents, one file each: `dicom`, `notes`, `tabular`. The file names the skills it carries and the tools it is granted. |
| `servers/` | MCP servers built for MedDataOS: `totalseg` (TotalSegmentator), runnable from a pixi environment or as an image. |
| `configs/` | `harness.toml` (local overrides for agents), `mcp.json` (MCP servers, managed with `pixi run mcp`), `references.toml` (reference sites). |
| `evals/` | Eval questions and the synthetic case `SYN-EVAL-01`. |
| `examples/agent.py` | An example external agent for `--agent-url`. |
| `tests/` | Offline tests, mirroring the package. `pixi run test`. |
| `docs/` | This documentation. |
