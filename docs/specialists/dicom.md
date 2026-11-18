# DICOM specialist

The DICOM specialist answers imaging questions with the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/sandbox_agents). It is a `SandboxAgent` behind an OpenAI-compatible endpoint, and it has three sources:

- **The case.** It reads the case only through the datactrl tools MedDataOS sends with each request (`dicom_tree`, `dicom_groups`, `describe`, `fetch`, and the rest). MedDataOS runs those tools, so citations work as they do for any other agent.
- **Reference sites.** `reference_search` and `reference_read` cover an allowlist of radiology sites in `configs/dicom_references.toml`, starting with [Radiology Assistant](https://radiologyassistant.nl/). They give background on anatomy, classification and staging systems, and differentials.
- **A sandbox.** It runs Python in a shell with numpy, scipy, SimpleITK, nibabel, pydicom, scikit-image, pandas and matplotlib, on the volumes and previews it fetched. Use it for structure volumes, HU statistics and sizes.

## Run it

```bash
pixi run dicom                 # installs the sandbox env on first run, serves :8005
pixi run stack /path/to/case   # or start it with the rest of the stack
```

It is registered in `configs/agents.toml`:

```toml
[dicom]
url = "http://127.0.0.1:8005/v1"
description = "DICOM and radiology specialist. ..."
```

The router calls it as `consult_dicom`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AZURE_OPENAI_*` | | The model, the same settings the other specialists use. |
| `DICOM_AZURE_DEPLOYMENT` | `AZURE_OPENAI_DEPLOYMENT` | Deployment for this specialist. It must accept function tools and images. |
| `DICOM_API` | `chat` | `chat` for Chat Completions, or `responses` for the Azure `/openai/v1` Responses API. |
| `DICOM_SANDBOX` | `local` | Where sandbox commands run. Only `local` exists today; `docker` is reserved. |
| `DICOM_MAX_TURNS` | `24` | Model turns allowed inside one request. |
| `AGENT_TIMEOUT` | `120` | The MedDataOS timeout per request. Sandbox work happens inside one request, so set it to about `300`. |

## How a consult runs

MedDataOS POSTs the system prompt, the question, the datactrl tools and the history so far. Each POST is one Agents SDK run:

1. The agent may call the sandbox (`exec_command`, `view_image`) and the reference tools as often as it needs.
2. If it calls a MedDataOS tool, the run stops. The calls go back as `tool_calls`, MedDataOS runs them, and then it POSTs again with the results.
3. Plain text ends the consult. MedDataOS then checks its citations.

Base64 payloads in tool results never reach the model. That covers `volume_base64` (`.nii.gz`), `image_base64` (PNG previews) and `pdf_base64`. Each one is written to the consult's workspace, and the model gets its `workspace_path` in place of the payload.

## Sandbox

There is one workspace per consult, under `$SPECIALISTS_CACHE/dicom/workspaces/<key>/`, with these folders: `volumes/`, `previews/`, `documents/`, `scripts/` and `out/`. A workspace lasts for all the requests of one consult and is deleted after it has been idle for 2 hours.

Commands run through the SDK's `UnixLocalSandboxClient`:

- The `dicom-sandbox` pixi environment is first on `PATH`.
- API keys and tokens are blanked from the environment.
- `HOME` is the workspace.

Add packages, such as model runtimes, to `[feature.dicom-sandbox.dependencies]` in `pixi.toml`.

!!! warning
    The local sandbox is not a security boundary: commands run as your user on this machine. Use it on your own workstation only. The Docker client (`DockerSandboxClient` with `network_mode="none"`) is the planned replacement, and switching to it only changes the run config in `sandbox.py`.

## Reference sites

`configs/dicom_references.toml` lists the sites. Each site is indexed from its sitemap, or from the links on its home page, and the index is cached for 7 days under `$SPECIALISTS_CACHE/dicom/references`. Searches run on that local index, so search terms never leave the machine. Only pages on a listed host are fetched.

The agent is told to treat references as background and to cite claims about the patient only to case data.
