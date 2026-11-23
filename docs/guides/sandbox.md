# Turn on the sandbox

The sandbox lets the model run Python on the case: measure a contoured volume, compute statistics on a table, draw a figure and look at it. Without it the model can read files but not compute on them, and it says so when a question needs a measurement.

## What you need

- Docker.
- gVisor (`runsc`), the container runtime the sandbox uses by default. It runs each container on its own kernel, so model-written code never touches the host kernel. Install it with the [gVisor guide](https://gvisor.dev/docs/user_guide/install/) and register it with Docker; `docker info` should then list `runsc` under Runtimes.
- The sandbox's Python environment:

    ```bash
    pixi run dicom-sandbox-install
    ```

    It has numpy, scipy, SimpleITK, nibabel, pydicom, scikit-image, pandas and matplotlib, and is mounted into each container read-only, so no image is built.

## Turn it on

In `.env`:

```bash
HARNESS_SANDBOX=docker
```

Restart the server. The startup output says either `Sandbox: ready (runsc)` or why it is off.

### Without gVisor

The sandbox refuses to fall back to Docker's default runtime on its own. If you cannot install gVisor, name the runtime yourself:

```bash
HARNESS_SANDBOX_RUNTIME=runc
```

`runc` containers share the host kernel, so a kernel exploit in model-written code could reach the host. Use it on a development machine, not on a server holding real patient data.

## What the code can do

Each `exec` call is one fresh container:

- no network;
- the case folder read-only at `/case`;
- the session workspace read-write at `/workspace`, holding the volumes, previews and PDFs the model fetched (`volumes/`, `previews/`, `documents/`), its scripts (`scripts/`) and outputs (`out/`);
- no capabilities, a read-only root, and limits on memory (`HARNESS_SANDBOX_MEMORY`, default `4g`), CPUs (`HARNESS_SANDBOX_CPUS`, default `2`) and processes;
- a time limit (`HARNESS_SANDBOX_TIMEOUT`, default 300 s), after which the container is killed.

The model can cite a number a script printed as a derived value; the server checks it against the run's recorded output.

## Workspaces

Each conversation has a workspace under `.cache/harness/workspaces/<session>/` (or under `HARNESS_CACHE`). Fetched volumes are saved there directly, up to 512 × 512 × 1024 voxels. Workspaces hold data derived from the case and are not deleted automatically yet; remove old ones when you no longer need them.
