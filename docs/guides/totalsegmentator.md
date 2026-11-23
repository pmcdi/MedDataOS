# Set up TotalSegmentator

[TotalSegmentator](https://github.com/wasserth/TotalSegmentator) segments 117 anatomical structures on CT: vertebrae, the spinal canal, organs, vessels, muscles. With it, the model can answer questions such as "at which vertebral level is the tumour?" or "how large is the thyroid?". It finds anatomy only, never tumours: a tumour volume comes from the case's own contours (RTSTRUCT).

It runs as an MCP server in a container with no network. The DICOM agent grants it (`mcp__totalseg__*` in `agents/dicom.md`). The main model does not see it.

## What you need

- Docker, with gVisor or `HARNESS_SANDBOX_RUNTIME=runc` set (see [the sandbox guide](sandbox.md#without-gvisor)). The sandbox itself does not have to be on.
- The TotalSegmentator environment, about 3 GB, CPU only:

    ```bash
    pixi install -e totalseg
    ```

- The weights for the `total` task in `~/.totalsegmentator/nnunet/results`. The server never downloads them, because its container has no network. If you have run TotalSegmentator on this machine before, they are already there. Otherwise run it once outside MedDataOS, which downloads them:

    ```bash
    pixi run -e totalseg TotalSegmentator -i some_ct.nii.gz -o /tmp/seg --fast
    ```

    Note that TotalSegmentator sends anonymous usage statistics by default when run directly like this. The MedDataOS server switches them off.

## Turn it on

It is registered in the project scope (`configs/mcp.json`). A project server starts only once you approve it on your machine, so review it and approve it:

```bash
pixi run mcp get totalseg
pixi run mcp trust totalseg
```

Start the server; TotalSegmentator connects in the background, which takes a few seconds while torch loads. Check it:

```bash
curl -s http://127.0.0.1:8080/api/health
```

```json
{"status": "healthy", "mcp": {"totalseg": "connected (2 tools)"}, ...}
```

A status starting with `awaiting approval:`, `failed:` or `refused:` says why; see [Troubleshooting](troubleshooting.md#an-mcp-server-is-not-connected).

## Using it

Ask about anatomy on a CT, for example:

- "At which cervical vertebral level is the primary tumour?"
- "What is the thyroid volume on the planning CT?"
- "Which vertebrae does the scan cover?"

The DICOM agent fetches the CT as a volume, calls TotalSegmentator with the structures it needs, and cites the result as a derived value. The page shows the model and version that produced it.

What each result includes:

- the volume in mL;
- the centroid and superior-inferior extent, in the scan's world coordinates (mm);
- whether the scan edge cuts the structure off. Then its volume is only the part inside the scan, and the answer should say so. The spinal cord and oesophagus in a neck CT usually are cut off.

## Things to know

- **Speed.** About 70 to 80 seconds per call at full resolution for a neck CT on 12 CPU cores. The model can ask for the 3 mm `fast` mode (about 30 s), which is coarser.
- **The vertebrae are always segmented too.** When asked for a single small structure, TotalSegmentator crops tightly around it and loses context: on one case the thyroid alone came out at 2.4 mL against 12.3 mL from a full uncropped run. Adding the vertebrae keeps the crop wide (11.8 mL).
- **`spinal_cord` covers the spinal canal's contents**, not the cord alone, so its volume is larger than the cord's.
- **Vertebral levels compare centroids.** Neighbouring vertebrae overlap in extent, so the skill tells the model to compare centroids.
- **Limits and resources** are in its registration: a 1800 s timeout, 12 GB of memory and 12 CPUs. On a smaller machine, register a local copy with lower limits (`pixi run mcp add totalseg --scope local ...`), which overrides the project one.
- **As an image.** `servers/totalseg/Dockerfile` builds the server into an image, CPU by default or on the official `wasserth/totalsegmentator` image for a GPU. Register it with `--image` in the local scope to use it instead; see `servers/totalseg/README.md`.
