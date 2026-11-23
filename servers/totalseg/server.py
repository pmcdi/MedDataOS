"""TotalSegmentator as an MCP server: anatomy masks and their measurements.

Run by the harness over stdio (``pixi run -e totalseg totalseg-mcp``), normally
inside the sandbox container: no network, the session workspaces mounted, the
weights mounted read-only. It finds anatomy (117 structures in the ``total``
task: organs, vertebrae, vessels, spinal cord). It does not find tumours; a
GTV comes from the case's RTSTRUCT.

Settings:

    TOTALSEG_WEIGHTS_PATH  folder holding the Dataset29x_* weight folders
                           (default ~/.totalsegmentator/nnunet/results)
    TOTALSEG_HOME_DIR      where TotalSegmentator keeps its config and counter
                           (default: a private temporary folder)
    TOTALSEG_THREADS       torch threads (default: every CPU)

Usage statistics are switched off before TotalSegmentator is imported: its
default sends a machine id and counter to stats.totalsegmentator.com on each
run. The container has no network as well.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from difflib import get_close_matches
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from pydantic import Field

TASK = "total"
WEIGHTS_RELEASE = "v2.0.0-weights"
# Parameters the harness turns into absolute workspace paths.
PathArgument = Annotated[str, Field(json_schema_extra={"format": "path"})]


def _private_home() -> None:
    """A TotalSegmentator home with usage statistics off, before it is imported."""
    home = Path(
        os.environ.get("TOTALSEG_HOME_DIR") or tempfile.mkdtemp(prefix="totalseg-")
    )
    home.mkdir(parents=True, exist_ok=True)
    os.environ["TOTALSEG_HOME_DIR"] = str(home)

    config_file = home / "config.json"
    config = json.loads(config_file.read_text()) if config_file.exists() else {}
    # The disclaimer key only stops the notice; send_usage_stats stops the sending.
    config.update({"send_usage_stats": False, "statistics_disclaimer_shown": True})
    config.setdefault("totalseg_id", "meddataos")
    config.setdefault("prediction_counter", 0)
    config_file.write_text(json.dumps(config, indent=4))

    if "TOTALSEG_WEIGHTS_PATH" not in os.environ:
        os.environ["TOTALSEG_WEIGHTS_PATH"] = str(
            Path.home() / ".totalsegmentator" / "nnunet" / "results"
        )


_private_home()

from importlib.metadata import version  # noqa: E402

import nibabel as nib  # noqa: E402 - after the home is set up
import numpy as np  # noqa: E402
import torch  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from totalsegmentator.map_to_binary import class_map  # noqa: E402
from totalsegmentator.python_api import totalsegmentator  # noqa: E402

STRUCTURES = list(class_map[TASK].values())
# Segmented beside what is asked for, so the crop keeps its anatomical context.
# With roi_subset TotalSegmentator crops to the requested structures; on a tight
# crop the model loses context. On RADCURE-0006 the thyroid alone came out at
# 2.4 mL against 11.7 mL with the spine in the crop and 12.3 mL from the full,
# uncropped run (12 minutes on a CPU). Vertebrae outside the scan add nothing.
CONTEXT = [name for name in STRUCTURES if name.startswith("vertebrae_")]
TOTALSEG_VERSION = version("TotalSegmentator")

torch.set_num_threads(int(os.environ.get("TOTALSEG_THREADS") or os.cpu_count() or 1))

server = MCPServer(
    "totalseg",
    version=TOTALSEG_VERSION,
    instructions=(
        "TotalSegmentator anatomy segmentation for CT. It does not segment tumours."
    ),
)


@server.tool()
def list_structures(contains: str = "") -> dict[str, Any]:
    """Names of the anatomy TotalSegmentator can segment on CT, optionally filtered.

    Use these names in segment's structures. Tumours are not among them.
    """
    wanted = contains.strip().lower()
    names = [name for name in STRUCTURES if wanted in name]
    return {"task": TASK, "count": len(names), "structures": names}


@server.tool()
def segment(
    image_path: PathArgument,
    structures: list[str],
    out_dir: PathArgument = "out/totalseg",
    fast: bool = False,
) -> dict[str, Any]:
    """Segment anatomy on a CT volume and measure each structure.

    image_path is a CT NIfTI in the workspace, such as one fetch saved under
    volumes/. structures are names from list_structures (spinal_cord,
    vertebrae_C4, thyroid_gland, ...). Masks are written under out_dir. Returns
    each structure's volume in mL, voxel count, centroid and superior-inferior
    extent in the image's world coordinates (mm), and whether the scan edge cuts
    it (then its volume is only the part inside the scan). Vertebrae overlap in
    superior-inferior extent, since their processes reach below the next body;
    compare centroids to name a level. spinal_cord covers the spinal canal's
    contents, not the cord alone. fast uses the 3 mm model: quicker, coarser.
    Full resolution takes about a minute for a neck CT on a CPU.
    """
    image = Path(image_path)

    if not image.is_file():
        raise ToolError(f"No image at {image_path}. Fetch the CT as a volume first.")

    if not image.name.endswith((".nii", ".nii.gz")):
        raise ToolError(f"{image.name} is not a NIfTI volume (.nii or .nii.gz).")

    unknown = [name for name in structures if name not in STRUCTURES]

    if not structures or unknown:
        hints = {name: get_close_matches(name, STRUCTURES, n=3) for name in unknown}
        raise ToolError(
            "Name structures from list_structures. "
            + "; ".join(
                f"{name}: did you mean {', '.join(close) or 'nothing close'}"
                for name, close in hints.items()
            )
        )

    run_dir = Path(out_dir) / f"run-{uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    totalsegmentator(
        input=image,
        output=run_dir,
        task=TASK,
        roi_subset=list(dict.fromkeys([*structures, *CONTEXT])),
        fast=fast,
        device="cpu",
        quiet=True,
    )

    seconds = round(time.monotonic() - started, 1)
    measured = {name: measure(run_dir / f"{name}.nii.gz") for name in structures}
    resolution = "3 mm" if fast else "full resolution"
    model = f"TotalSegmentator {TOTALSEG_VERSION}, task {TASK}, {resolution}"

    return {
        "summary": summary_line(model, measured),
        "model": "TotalSegmentator",
        "version": TOTALSEG_VERSION,
        "weights": WEIGHTS_RELEASE,
        "task": TASK,
        "fast": fast,
        "image": str(image),
        "masks_dir": str(run_dir),
        "seconds": seconds,
        "structures": measured,
    }


def measure(mask_path: Path) -> dict[str, Any]:
    """Volume, extent and centroid of one binary mask, in world millimetres."""
    if not mask_path.is_file():
        return {"found": False, "volume_ml": 0.0, "mask": None}

    image = nib.load(mask_path)
    mask = np.asarray(image.dataobj) > 0
    voxels = int(mask.sum())

    if voxels == 0:
        return {"found": False, "volume_ml": 0.0, "mask": str(mask_path)}

    zooms = image.header.get_zooms()[:3]
    voxel_ml = float(np.prod(zooms)) / 1000.0
    indices = np.argwhere(mask)
    world = nib.affines.apply_affine(image.affine, indices)

    # The slice axis is the one most aligned with the world's superior axis.
    slice_axis = int(np.argmax(np.abs(image.affine[2, :3])))
    first, last = indices[:, slice_axis].min(), indices[:, slice_axis].max()
    cut = bool(first == 0 or last == mask.shape[slice_axis] - 1)

    return {
        "found": True,
        "volume_ml": round(voxels * voxel_ml, 2),
        "voxels": voxels,
        "centroid_mm": [round(float(value), 1) for value in world.mean(axis=0)],
        "superior_mm": round(float(world[:, 2].max()), 1),
        "inferior_mm": round(float(world[:, 2].min()), 1),
        "cut_by_scan_edge": cut,
        "mask": str(mask_path),
    }


def summary_line(model: str, measured: dict[str, dict[str, Any]]) -> str:
    """One line a person can read: each structure's volume, with its caveats."""
    parts = []

    for name, found in measured.items():
        if not found["found"]:
            parts.append(f"{name} not found")
            continue

        note = " (cut by scan edge: partial)" if found["cut_by_scan_edge"] else ""
        parts.append(f"{name} {found['volume_ml']} mL{note}")

    return f"{model}: " + "; ".join(parts)


if __name__ == "__main__":
    server.run()
