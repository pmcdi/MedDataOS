"""The package's layers: each imports only the layers beneath it.

    core/datactrl   the controller: imports nothing else from meddataos
    core/           the loop, model, registry, workspace, verifier: no harness, no server
    harness/        capabilities plugged into core: no server
    server/         HTTP and sessions: anything

datactrl is installed on its own (the ``datactrl`` extra), so importing it must
not load the runtime's packages either.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "meddataos"

# Layer -> package prefixes it must not import.
FORBIDDEN = {
    "core/datactrl": (
        "meddataos.core.loop",
        "meddataos.core.model",
        "meddataos.core.registry",
        "meddataos.core.workspace",
        "meddataos.core.config",
        "meddataos.core.verifier",
        "meddataos.harness",
        "meddataos.server",
        "scripts",
    ),
    "core": ("meddataos.harness", "meddataos.server", "scripts"),
    "harness": ("meddataos.server", "scripts"),
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)

    return found


def layer_files(layer: str) -> list[Path]:
    files = sorted((PACKAGE / layer).rglob("*.py"))

    # core's own rule leaves out datactrl, which has the stricter one above.
    if layer == "core":
        files = [
            path for path in files if "datactrl" not in path.relative_to(PACKAGE).parts
        ]

    return files


@pytest.mark.parametrize("layer", list(FORBIDDEN))
def test_each_layer_imports_only_the_layers_beneath_it(layer):
    breaches = [
        f"{path.relative_to(PACKAGE)} imports {module}"
        for path in layer_files(layer)
        for module in imported_modules(path)
        if module.startswith(FORBIDDEN[layer])
    ]

    assert not breaches, "\n".join(breaches)


def test_the_controller_imports_without_the_runtime():
    # A fresh interpreter: this test process has already imported everything.
    probe = (
        "import sys, meddataos.core.datactrl, meddataos.core.datactrl.tools\n"
        "loaded = [name for name in ('httpx', 'mcp', 'meddataos.core.loop', "
        "'meddataos.core.verifier', 'meddataos.harness') if name in sys.modules]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == ""
