# Define an agent

An agent is a subagent: the same tool loop in a fresh context, with one task, the skills it carries, and only the tools it is granted. It does the reading away from the main conversation and returns checked findings the main model cites.

Agents are files in `agents/<name>.md`. Three ship with MedDataOS, one per domain: `dicom`, `notes` and `tabular`. Adding an agent is one file, and an agent may carry several skills. There is no automatic agent per skill.

The main model, the router, is not an agent file. Its prompt stays in code. It gets every harness tool, plus MCP servers marked `router`.

## The file

```markdown
---
name: dicom
description: Scans and series - metadata, measurements, anatomy segmentation, radiology background
skills: dicom
tools: dicom_tree, dicom_groups, links, assert_link, describe, fetch, exec, view_image,
  reference_search, reference_read, mcp__totalseg__*, mcp__mira-neck__*
max_steps: 24
---
```

The body under the front matter is optional. The three domain agents leave it empty and let the skill speak.

- **`name`**: the file is `agents/<name>.md`, and the front matter name matches it. `spawn_subagent` takes this name.
- **`description`**: one line, shown to the main model on `spawn_subagent` so it knows when to call this agent.
- **`skills`**: skills to preload, in order. Each must exist.
- **`tools`**: the only grant. A plain tool name, an exact `mcp__<server>__<tool>`, or `mcp__<server>__*` for every tool of that server. A plain name has to be a case or harness tool. `spawn_subagent` cannot be granted: a subagent cannot spawn, so the grant would do nothing.
- **`max_steps`**: tool turns before it must answer. Default 16. `0` is an error in the file and in `[agent.<name>]`, with the same message.
- **`model_api`**, **`model`**, **`reasoning_effort`**: optional. Prefer [`[agent.<name>]`](../reference/harness-config.md) for these, so a shared file does not hard-code one deployment.

## What it is told

The system prompt is, in order:

1. The subagent rules: cite case files, do not start another agent, the answer schema.
2. The agent's own body, when it has one.
3. The body of each skill it lists.

So a DICOM question's agent sees the DICOM skill already, and does not call `load_skill` for it.

## Grants

The `tools` line is the only grant. MCP registrations do not name agents or skills. They only say whether the router gets the server's tools (`router`).

A grant is a permission, not a dependency. A pattern expands when the server has connected: `mcp__totalseg__*` becomes that server's tools and no others. A tool that is granted but unavailable is not offered. The sandbox being off drops `exec` and `view_image`. A server that has not connected drops its tools. The agent is told, in the skill, to say so rather than guess.

A grant may name a server this checkout has not registered, such as one that lives only in a gitignored `configs/mcp.local.json`. Startup logs a warning once and continues:

```text
agents/dicom.md grants mira-neck, which is not registered here; its tools are not offered
```

The grant is kept. Until that server is registered and connected, it expands to nothing, the same as a registered server that has not connected. An unknown plain tool still stops startup.

Each skill's `requires` must be covered by the grant, after patterns expand. `mcp__totalseg__*` covers `mcp__totalseg__segment`. A gap stops the server at startup and names the file. So does an unknown skill, an unknown plain tool, granting `spawn_subagent`, or a `[agent.<name>]` table with no file. A skill's `uses` list is optional and is not checked.

## How the router chooses

`spawn_subagent` lists each agent with the notable tools it can use right now, generated from its grants. That list is MCP servers that have connected, named by their registration name, plus `exec` and `view_image` when the sandbox can run. Tools every agent has, such as `describe` and `fetch`, are left out. A server that is down or not registered drops out of the list, so the line stays true as grants and connections change.

The router is given one rule. Launch an agent when it has tools the router does not (segmentation models, code execution for its domain), or when the work means many reads of one kind. Do a quick lookup itself when its own tools can finish it. Launch several agents in the same turn when a question spans areas; they run in parallel. Use `load_skill` for guidance it can follow with its own tools.

When the router loads a skill and lacks a tool from its `requires` or `uses` that an agent can use right now, the reply keeps the instructions and adds a note naming that agent. The DICOM skill's `uses` line is how segmentation is offered: the router does not have those servers, and the note names the DICOM agent once they are connected. The note follows what is available, not the grant. With the sandbox off it does not say the agent can use `exec`. When no agent can use a missing tool, the note says why: the sandbox is off, that server is not connected, or no agent grants it.

## Local overrides

`configs/harness.toml` holds overrides only. They win over the file:

```toml
[agent.dicom]
max_steps = 32
reasoning_effort = "high"
# enabled = false    # the skill stays loadable; this agent is not offered
```

`enabled = false` keeps `load_skill` and drops the agent from `spawn_subagent`. A leftover `[subagent.*]` table stops startup: those tables moved here and to `agents/`.

## Budgets and finding ids

Agents of one question share a budget: 3 runs of one agent, 8 runs in all. The same task, context and resources asked twice returns the first answer and does not count again.

Findings are keyed by the agent name. The first checked citation from the DICOM agent is `dicom.1`, the next `dicom.2`. The main model cites them as `[cite:dicom.1]` and does not copy the citation.
