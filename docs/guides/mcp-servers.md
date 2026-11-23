# Add an MCP server

An MCP server gives the model tools that run in another process: a segmentation model, a dose calculator, a literature search. MedDataOS is an MCP client. You register servers the way you do in Claude Code, with a command and a JSON file in its `mcpServers` format, and MedDataOS adds what a patient-data tool needs: where the server runs, whether what it returns can be cited, and who approved it.

TotalSegmentator (`servers/totalseg/`) is a working example.

## 1. Write or pick the server

Any MCP server over stdio or streamable HTTP works. A minimal Python one with the `mcp` package:

```python
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("dose")

@server.tool()
def mean_dose(dose_path: str, mask_path: str) -> dict:
    """Mean dose in Gy inside a structure mask."""
    ...
    return {"summary": "Mean dose 41.2 Gy", "mean_gy": 41.2}

if __name__ == "__main__":
    server.run()
```

Three things make a server work well here:

- **Return a `summary` line** in the result. The page's trace shows it.
- **Raise `ToolError` for problems the model should see.** Its message reaches the model; other exceptions are hidden by MCP.
- **Mark file parameters as paths**, by naming them `*_path` or `*_dir` (or `"format": "path"` in the schema). MedDataOS turns a workspace path such as `volumes/ct.nii.gz` into an absolute path for those parameters, and refuses one that leaves the session workspace.

Package a server with heavy dependencies as a container image, or give it its own pixi environment, so its dependencies stay out of MedDataOS's.

## 2. Register it

```bash
pixi run mcp add dose --evidence derived --sandbox \
    --python-env .pixi/envs/dose --timeout 600 \
    -- python servers/dose/server.py
```

or, for a server packaged as an image:

```bash
pixi run mcp add dose --image lab/dose-mcp:1.0 --evidence derived
```

or, for one that runs elsewhere:

```bash
pixi run mcp add literature --transport http --url http://127.0.0.1:9000/mcp
```

The options that matter most:

- **`--evidence`** decides how the verifier treats its output. `derived`: a value it returns can be cited and is checked against the recorded result. `background` (the default) or `navigation`: never cited.
- **`--router`** offers the server's tools to the main model. Leave it off for a tool an agent should call: grant `mcp__dose__*` (or one tool's name) in that agent's file. Registrations no longer name skills.
- **`--sandbox`** (implied by `--image`) runs it in a container with no network, the session workspaces as the only writable folder, and its environment and `--mount`s read-only. Use it for anything that touches patient data. If no container can start, the server is refused rather than run on the host. An image is never pulled for you: build or pull it first.

`pixi run mcp --help` lists the rest; [MCP registration](../reference/mcp-registration.md) describes every key.

## Scopes

Like Claude Code, a registration has a scope:

| Scope | File | For |
|---|---|---|
| `local` (default) | `configs/mcp.local.json`, not committed | Trying a server in this checkout |
| `project` | `configs/mcp.json`, committed | Servers everyone working on this repository uses |
| `user` | `~/.meddataos/mcp.json` | Your servers, on every checkout |

`--scope` picks one. When two scopes register the same name, the narrower wins: local over project over user. The files are not called `.mcp.json` on purpose: Claude Code reads that name, and would offer your patient-data servers to whoever is coding in the repository.

## 3. Approve it

A project server can be added or changed by anyone who commits to the repository, so it does not start until you approve it on your machine:

```bash
pixi run mcp get dose      # what it runs, its evidence, mounts and env keys
pixi run mcp trust dose
```

An edit to its entry, such as granting `derived` evidence or dropping the sandbox, needs approval again. Servers you registered yourself (local and user scope) need none.

## 4. Start and check

Restart the server and check `/api/health`, or the startup log:

```bash
curl -s http://127.0.0.1:8080/api/health
```

`"dose": "connected (1 tools)"` means its tools are available, named `mcp__dose__<tool>`. The main model sees them only when the registration has `router` set. Otherwise grant them in the agent that should call them, and say when, in that skill's `SKILL.md` ([Write a skill](skills.md), [Define an agent](agents.md)).

`pixi run mcp list` shows every server, its scope, where it runs, its evidence and how far it is trusted.

## Pinned tools

The first time a server connects, its tool descriptions and schemas are pinned. If they change later, for example after an update, the server is refused with a list of what changed:

```text
refused: its tools differ from the trusted ones (changed: mean_dose). Review the server, then run: pixi run mcp trust dose
```

A tool description is text the model follows, so a changed description is a changed prompt. Read the change, then trust it again with `pixi run mcp trust dose`. Register a server you are developing with `--no-pin` to skip this.

## Measured: mira-neck

mira-neck is registered in the local scope only (`configs/mcp.local.json`, image `mira-neck-mcp:0.1.1`, evidence `derived`). It is not offered to the router. The DICOM agent grants `mcp__mira-neck__*`. Its registration is 12 GB and 12 CPUs. That is the limit the measurement supports: on the RADCURE-0006 planning CT (181 slices, 0.89×0.89×2 mm), through `McpHub` with `HARNESS_SANDBOX_RUNTIME=runc`,

- `segment_neck_levels` took 272 s and saturated the 12 CPUs. cgroup `memory.peak` was 9.01 GiB. The 4 GB sandbox default is killed on the first sliding window (about 4.2 GB).
- `levels_of_structure` on that run's label map and the GTVp mask took 0.5 s. The mask is 2.57 mL on the CT grid, centroid at z −997 mm. None of its voxels fall in a segmented level (100% outside, no centroid level). The same grid holds the levels, from z −1058 mm to −834 mm. The model segments IA, IB, II, III, IV, V and VIIA, not level VI, which is where a glottic primary sits.

## When a server misbehaves

- A tool error or a call past its timeout comes back to the model as a failed tool call; the question continues.
- A server that crashes is reconnected with backoff; its status shows `reconnecting` meanwhile.
- A sandboxed server the kernel kills for memory is not retried. The status and the tool error say it ran out of memory and name the limit it was given, so that limit can be raised in its registration.
- A server that never connects offers nothing, and the rest of the harness works as usual. [Troubleshooting](troubleshooting.md#an-mcp-server-is-not-connected) lists the statuses.
