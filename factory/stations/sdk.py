"""Station 6: deterministic Python client SDK via openapi-python-client."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from factory.harness import sandbox


def generate_sdk(project: Path, timeout_s: float) -> sandbox.CommandResult:
    out = project / "client"
    if out.exists():
        shutil.rmtree(out)
    result = sandbox.run(
        [
            sys.executable, "-m", "openapi_python_client", "generate",
            "--path", "openapi.yaml",
            "--output-path", "client",
            "--meta", "none",
            "--overwrite",
        ],
        cwd=project,
        timeout_s=timeout_s,
        env={
            "PATH": f"{Path(sys.executable).parent}:{sandbox.scrubbed_env().get('PATH', '')}",
            "RUFF_NO_CACHE": "true",  # its ruff post-hooks would otherwise cache in the project
        },
    )
    if not result.ok:
        return result
    # The SDK must at least import cleanly with the project's dependencies.
    return sandbox.run(
        [str(project / ".venv/bin/python"), "-c", "import client; from client import Client"],
        cwd=project,
        timeout_s=timeout_s,
    )
