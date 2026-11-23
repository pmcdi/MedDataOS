# TotalSegmentator MCP server

TotalSegmentator's `total` task (117 anatomical structures on CT) as an MCP server for MedDataOS. Tools: `list_structures` and `segment`. It finds anatomy, never tumours.

It runs in a container with no network, one of two ways. Both mount the `total` weights from `~/.totalsegmentator/nnunet/results` read-only.

## From a pixi environment (the default registration)

```bash
pixi install -e totalseg
pixi run mcp trust totalseg        # it is registered in configs/mcp.json
```

The container is a plain Debian image with the environment mounted read-only.

## From an image

```bash
docker build -t meddataos/totalseg-mcp:2.18.0 servers/totalseg
pixi run mcp add totalseg --scope local --image meddataos/totalseg-mcp:2.18.0 \
    --evidence derived --timeout 1800 --memory 12g --cpus 12 \
    --mount ~/.totalsegmentator/nnunet/results:/weights:ro
```

A local registration overrides the project one with the same name. The image is never pulled automatically. See the Dockerfile for a GPU build on the official `wasserth/totalsegmentator` image.

See `docs/guides/totalsegmentator.md` for what it returns and its limits.
