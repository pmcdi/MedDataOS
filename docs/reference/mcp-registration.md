# MCP registration

MCP servers are registered in JSON files in Claude Code's `mcpServers` format. `pixi run mcp add` writes them; you can also edit them by hand. A running server reads them at startup.

## Files and scopes

| Scope | File | Override with |
|---|---|---|
| `user` | `~/.meddataos/mcp.json` | `HARNESS_MCP_USER` |
| `project` | `configs/mcp.json` (committed) | `HARNESS_MCP_PROJECT` |
| `local` | `configs/mcp.local.json` (gitignored) | `HARNESS_MCP_LOCAL` |

On a name registered in more than one scope, local wins over project, and project over user. A file that is not valid, or an entry that is not, stops the server at startup with the file and entry named.

## An entry

```json
{
  "mcpServers": {
    "totalseg": {
      "command": "python",
      "args": ["servers/totalseg/server.py"],
      "env": {"TOTALSEG_WEIGHTS_PATH": "/weights"},
      "meddataos": {
        "evidence": "derived",
        "timeout": 1800,
        "sandbox": true,
        "python_env": ".pixi/envs/totalseg",
        "mounts": ["~/.totalsegmentator/nnunet/results:/weights:ro"],
        "memory": "12g",
        "cpus": "12"
      }
    },
    "literature": {"type": "http", "url": "http://127.0.0.1:9000/mcp"}
  }
}
```

### Claude Code's keys

| Key | What it does |
|---|---|
| `type` | `stdio` (default) or `http`. |
| `command`, `args` | The command that starts a stdio server, and its arguments. Repository paths are made absolute. With an `image`, both may be left out to run the image's entrypoint. |
| `url` | An http server's endpoint. |
| `env` | Environment for the server: inside its container when sandboxed. |

`${VAR}` and `${VAR:-default}` in these are filled from the environment. `headers` is not supported yet.

### `meddataos`

| Key | Default | What it does |
|---|---|---|
| `evidence` | `background` | `derived`: values it returns can be cited and are checked against the recorded result. `background` or `navigation`: never cited. `case` is refused for now. |
| `router` | `false` | `true`: the main model gets this server's tools. Agents grant themselves, in `agents/<name>.md` (`mcp__<server>__*` or one tool's name). `skills` is no longer read. |
| `timeout` | `300` | Seconds per call. |
| `files` | `workspace` | `workspace`: path parameters become absolute paths in the session workspace. `none`: arguments pass untouched, for a server on another machine. |
| `sandbox` | `false`; `true` with `image` | Run the server in a container: no network, the session workspaces as the only writable folder. Not with `http`. |
| `image` | | A container image holding the server. It runs sandboxed, keeps its own entrypoint, PATH and working folder, and is never pulled: it must be on the machine. |
| `python_env` | | A pixi environment, mounted read-only and first on `PATH` in the container. |
| `mounts` | `[]` | More mounts, `host[:container][:ro\|rw]`. The container path defaults to the host path, the mode to `ro`. `~` and repository-relative paths work. |
| `memory`, `cpus` | the sandbox's | Container limits. |
| `pin` | `true` | Refuse the server when its tool definitions change, until trusted again. |

A path parameter is one the server's schema marks with `"format": "path"`, or whose name is `path` or ends in `_path` or `_dir`. A server that marks none gets the older rule: any string starting with `volumes/`, `previews/`, `documents/`, `out/` or `scripts/`. A path that leaves the workspace is refused, and the workspace prefix is taken off what comes back.

## Trust

Two things are recorded in `$HARNESS_CACHE/mcp_trust.json`, per server:

- **Approval**, for project servers only: a hash of the whole entry as you approved it with `pixi run mcp trust <name>`. Until then, and after any edit to the entry, the server's status is `awaiting approval`.
- **Pinned tools**, for every server with `pin` on: a hash of each tool's description and schema, recorded on first connect. A difference refuses the server until `pixi run mcp trust <name>`.

`trust` approves the entry as it is now and forgets the pinned tools, which are pinned again on the next connect.

## `pixi run mcp`

| Command | What it does |
|---|---|
| `add NAME [options] -- COMMAND [ARGS]` | Register a server. Checked exactly as the server will read it before anything is written; refuses a name already in that scope. |
| `list` | Every server: name, scope, where it runs, evidence, whether the router gets it, trust. |
| `get NAME` | One server as an approval covers it, with env keys but not their values, and its JSON entry. |
| `trust NAME` | Approve it as registered now, and re-pin its tools. |
| `remove NAME [--scope S]` | Remove a registration; `--scope` when the name is in several. |

`add` takes `--scope`, `--transport`, `--url`, `--env NAME=value`, `--evidence`, `--router`, `--sandbox`, `--image`, `--python-env`, `--mount` (repeatable), `--timeout`, `--memory`, `--cpus`, `--files` and `--no-pin`. `--router` offers the server's tools to the main model. Without it, only an agent that grants the server sees them.
