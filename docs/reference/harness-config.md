# Harness config

`configs/harness.toml` holds local overrides for agents. The agent itself is `agents/<name>.md`. `HARNESS_CONFIG` points at another file. A table that names an agent with no file stops the server at startup, and so does a leftover `[subagent.*]` table: those moved to `[agent.*]` and `agents/`.

## `[agent.<name>]`

Overrides for one deployment. They win over the agent file, so a shared file never hard-codes a model.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | `false` keeps `load_skill` but does not offer the agent. |
| `model_api` | `HARNESS_MODEL_API` | `chat` or `responses`. |
| `model` | the harness model | Model or deployment name. |
| `reasoning_effort` | the harness setting | Reasoning effort for this agent. |
| `max_steps` | the file's, or `16` | Tool turns before it must answer. |

```toml
[agent.dicom]
max_steps = 32
reasoning_effort = "high"
```

The description, the skills and the tool grant live in the agent file. See [Define an agent](../guides/agents.md).

MCP servers are not here: they are registered in their own files. See [MCP registration](mcp-registration.md). An agent grants a server's tools in its own file. `router` on the registration offers them to the main model.

## `configs/references.toml`

The reference sites `reference_search` and `reference_read` may use.

```toml
[[site]]
name = "radiologyassistant"
base_url = "https://radiologyassistant.nl/"
max_depth = 0
```

| Key | What it does |
|---|---|
| `name` | The label the model passes as `site`. |
| `base_url` | Pages are fetched only from this host. |
| `sitemap` | Optional sitemap URL. Without one, the home page and the pages it links to (`max_depth` levels) are crawled to build the index. |
| `query_params` | Query parameters this site's pages need, such as `["page"]`. Every other parameter is dropped before a fetch. |

The search index is cached for 7 days under `$HARNESS_CACHE/references`; delete that folder to rebuild it after changing the file.
