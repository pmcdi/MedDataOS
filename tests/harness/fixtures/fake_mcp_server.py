"""A stand-in segmentation MCP server, spoken to over stdio by the tests.

``segment`` writes a mask where it is told and reports a volume, as a real
segmentation server would; the others fail, stall, or return a picture.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# The smallest valid PNG: one transparent pixel.
PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

server = MCPServer("fake-seg")


@server.tool()
def segment(image: str, out: str) -> str:
    """Segment the primary tumour in a volume and write its mask."""
    if not Path(image).is_file():
        raise ToolError(f"no volume at {image}")

    Path(out).write_text("mask")
    return f"GTVp volume: 2.25 mL\nmask: {out}"


@server.tool()
def measure(label: str) -> str:
    """Report a structure's volume, without any files, for the server tests."""
    return f"{label} volume: 2.25 mL"


@server.tool()
def broken() -> str:
    """Always fails, as a server with missing weights would.

    A ToolError's words reach the client; other exceptions are hidden by MCP.
    """
    raise ToolError("model weights missing")


@server.tool()
def stall(seconds: float) -> str:
    """Sleep, to stand in for a model that runs too long."""
    time.sleep(seconds)
    return "done"


@server.tool()
def crash() -> str:
    """Exit without answering, as a server that dies mid-call would."""
    os._exit(1)


@server.tool()
def oom() -> str:
    """Exit 137, the status the kernel's OOM killer leaves."""
    os._exit(137)


@server.tool()
def locate(image_path: str, label: str) -> str:
    """Report where a marked path argument arrived, beside an unmarked one."""
    return f"{image_path} | {label}"


@server.tool()
def overlay() -> Image:
    """Return a picture of the mask over the image."""
    return Image(data=PIXEL_PNG, format="png")


if __name__ == "__main__":
    server.run()
