# Settings

Settings are environment variables, read from `.env` at the repository root when the server starts. `.env.example` lists them with comments. Restart the server after a change.

## Model

| Variable | Default | What it does |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | | Azure resource URL. With the key set, Azure is used instead of `OPENAI_*`. |
| `AZURE_OPENAI_API_KEY` | | Azure key. |
| `AZURE_OPENAI_DEPLOYMENT` | | Deployment name, sent as the model. |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | API date Azure expects. |
| `AZURE_OPENAI_REASONING_EFFORT` | empty | Reasoning effort sent with each request; empty sends none. |
| `OPENAI_MODEL` | | Model name for OpenAI or a compatible server. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL, the part before `/chat/completions`. |
| `OPENAI_API_KEY` | | Bearer key; most local servers need none. |
| `OPENAI_REASONING_EFFORT` | empty | Reasoning effort for `OPENAI_*` models. |
| `HARNESS_MODEL_API` | `chat` | `chat` or `responses`. [Which to use](../guides/models.md#chat-completions-or-responses). |
| `HARNESS_MODEL_TIMEOUT` | `120` | Seconds to wait for one model response. |
| `HARNESS_MODEL_RETRIES` | `4` | Retries after 408, 429, 5xx, a dropped connection or a timeout. |

## Harness

| Variable | Default | What it does |
|---|---|---|
| `HARNESS_CONFIG` | `configs/harness.toml` | Subagent tables. [Harness config](harness-config.md). |
| `HARNESS_MCP_USER` | `~/.meddataos/mcp.json` | User-scope MCP registrations. [MCP registration](mcp-registration.md). |
| `HARNESS_MCP_PROJECT` | `configs/mcp.json` | Project-scope MCP registrations. |
| `HARNESS_MCP_LOCAL` | `configs/mcp.local.json` | Local-scope MCP registrations. |
| `HARNESS_CACHE` | `.cache/harness` | Session workspaces, the reference index, the notes ledger, MCP approvals and pinned tools. |
| `HARNESS_LEDGER` | `on` | `off` skips building the notes ledger; its tools are then not offered. |

## Sandbox

| Variable | Default | What it does |
|---|---|---|
| `HARNESS_SANDBOX` | `off` | `docker` turns on `exec` and `view_image`. |
| `HARNESS_SANDBOX_RUNTIME` | `runsc` | Container runtime for the sandbox and for MCP servers with `sandbox = true`. `runc` shares the host kernel. |
| `HARNESS_SANDBOX_IMAGE` | `debian:bookworm-slim` | Base image. |
| `HARNESS_SANDBOX_PYTHON_ENV` | `.pixi/envs/dicom-sandbox` | Python environment mounted into the container. |
| `HARNESS_SANDBOX_MEMORY` | `4g` | Memory limit per command. |
| `HARNESS_SANDBOX_CPUS` | `2` | CPU limit per command. |
| `HARNESS_SANDBOX_TIMEOUT` | `300` | Seconds before a command is killed. |

## Notes ledger

| Variable | Default | What it does |
|---|---|---|
| `GEMINI_API_KEY` | | The ledger's model when Azure is not set. Scanned-PDF OCR in the ledger needs Gemini. |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Gemini model for the ledger. |
| `NOTES_EXTRACT_MODEL`, `NOTES_REASON_MODEL` | | Override the ledger's extraction and reasoning models. |

## External agent

Used only with `--agent-url`.

| Variable | Default | What it does |
|---|---|---|
| `AGENT_MODEL` | | Sent to the agent as `model`. |
| `AGENT_API_KEY` | | Sent as a bearer token when set. |
| `AGENT_TIMEOUT` | `120` | Seconds to wait for each agent response. |

## Server and storage

| Variable | Default | What it does |
|---|---|---|
| `HOST` | `127.0.0.1` | Address the page binds to. |
| `PORT` | `8080` | Port the page binds to. |
| `MEDDATAOS_STORE` | `data/cases` | Where chats, pins, reviews and audit runs are saved. [Storage](storage.md). |
| `MEDDATA_CASES` | `data` | Where the eval runner looks for real cases. |

## Command line

```bash
pixi run serve <folder>                      # harness mode
pixi run serve <folder> --agent-url <url>    # an external agent instead
```

| Task | What it does |
|---|---|
| `pixi run serve` | Open a case and serve the page. |
| `pixi run agent` | Run the example external agent. |
| `pixi run eval` | Run the evals. [Guide](../guides/evals.md). |
| `pixi run mcp` | Register, list, inspect, remove and trust MCP servers. [MCP registration](mcp-registration.md). |
| `pixi run dicom-sandbox-install` | Install the sandbox's Python. |
| `pixi run test` | Run the offline tests. |
| `pixi run qc` | Lint and type checks. |
| `pixi run docs` | Serve these docs. |
