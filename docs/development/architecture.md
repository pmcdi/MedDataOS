# Code map

The package has three layers. Each layer imports only the layers beneath it, and `tests/core/test_layers.py` fails the build when one reaches up. The eval runner lives outside the package, in `scripts/evals`.

```
src/meddataos/
  core/                    what every answer passes through
    datactrl/              the controller: the only path to the case's files
    loop.py                the model-and-tools loop
    model.py               chat completions and Responses clients, retries
    registry.py            each tool's evidence class
    workspace.py           the session workspace; payloads kept out of context
    config.py              configs/harness.toml
    verifier/              an answer reaches the page only when every citation holds
      contract.py          the citation rule and its checks
      answer.py            one reply, judged against its question's reads
      schema.py            the citation JSON schema
      paths.py             how a citation names a case file
  harness/                 capabilities plugged into the core
    tools.py               the harness's own tools, offered beside the case tools
    sandbox.py             exec in a container
    mcp.py                 the MCP hub: containers, calls, reconnects
    mcp_config.py          MCP registration: scopes, files, approval, pinned tools
    mcp_cli.py             pixi run mcp
    references.py          allowlisted reference sites
    outbound.py            the patient-detail screen on outbound URLs
    skills.py              skills/<name>/SKILL.md, know-how only
    agents.py              agents/<name>.md: prompt, skills, tool grant
    ledger.py, notes/      the notes ledger
  server/                  HTTP and sessions
    server.py, routes.py   the app and its routes
    dispatch.py            a question: the conversation, subagents, corrections
    case.py                the open case: its tools and the reads they record
    state.py, store.py     sessions in memory; chats, pins and runs in SQLite
    findings.py            a subagent's checked findings
    timeline.py            the page's timeline
scripts/evals/             the eval runner and scoring (not part of the package)
servers/totalseg/          TotalSegmentator as an MCP server (its own pixi env)
skills/                    one SKILL.md per domain; requires and uses list tools, grants none
agents/                    one file per agent: dicom, notes, tabular
configs/                   harness.toml (agent overrides), mcp.json (MCP servers), references.toml
web/                       the page
```

## The layers

| Layer | May import | Why |
|---|---|---|
| `core/datactrl` | nothing else from `meddataos` | It is installed on its own (the `datactrl` extra, the `library` pixi environment). Importing it must not load httpx, mcp or the runtime. |
| `core/` | `core/datactrl` | The runtime and the verifier work without a web server or any capability, so they can be tested and reused alone. |
| `harness/` | `core/` | Capabilities are optional: a missing sandbox or MCP server removes tools, never breaks the core. |
| `server/` | everything | It wires the layers to HTTP, sessions and the page. |

`core/__init__.py` stays empty for the same reason as the first row: importing `meddataos.core.datactrl` must not import the rest of `core`.

## A question, by module

1. `server/routes.py` takes `POST /api/chat`; `server/dispatch.py` starts the question.
2. `dispatch.Conversation` (a `core/loop.ToolLoop`) asks the model through `core/model.py`.
3. Each tool call runs in `server/case.py` (case reads through `core/datactrl`), in `harness/tools.py` (references, skills, the ledger, the sandbox, MCP tools), or as an agent (`harness/agents.py`, run by `dispatch`).
4. Reads and derived runs are recorded under cite tokens in `server/case.py`. Large payloads go to the session workspace (`core/workspace.py`).
5. The reply is judged by `core/verifier` and sent back with the reason until it passes, at most six times.
6. `server/store.py` saves the chat and an audit run; the page gets the answer.

`Conversation` and the question still live in `server/dispatch.py`: they write trace steps, budgets and token use into the server's session state. Moving them into `core/` needs a session interface first.

## Where a change goes

| To add | Put it in |
|---|---|
| A case data format or view | `core/datactrl/adapters/` |
| A tool the harness runs itself | `harness/tools.py`, registered in `core/registry.py` with its evidence class |
| A model or tool from another process | An MCP server, registered with `pixi run mcp add`; its code in its own repository or image, or in `servers/<name>/` with its own pixi environment |
| Domain instructions | `skills/<name>/SKILL.md` |
| An agent (a subagent) | `agents/<name>.md` |
| A citation check | `core/verifier/contract.py` |
| A route or page behaviour | `server/` and `web/` |
| Eval scoring or the runner | `scripts/evals/`; questions stay in `evals/questions/` |

## Tests

`tests/` mirrors the layers: `core/` (with `core/datactrl/`), `harness/`, `server/` and `evals/`. `pixi run test` runs them all with no model and no network; `MEDDATAOS_TOTALSEG_E2E=1` adds the TotalSegmentator run on RADCURE-0006.
