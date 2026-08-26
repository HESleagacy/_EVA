"""Optional validation sidecar integration.

The Open XML SDK is a .NET library rather than a Python dependency. This
module accepts a locally installed validator command so validation remains
optional and reproducible without changing the native extraction path.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
from typing import Any


def validate_with_openxml_sdk(source: str | Path, command: str | None = None) -> dict[str, Any]:
    """Run an optional Open XML SDK validator sidecar.

    ``command`` may be supplied directly or through
    ``OPENXML_VALIDATOR_COMMAND``. The source path is appended as the final
    argument. The sidecar should return exit code 0 for a valid package.
    """
    configured = command or os.environ.get("OPENXML_VALIDATOR_COMMAND")
    if not configured:
        return {"available": False, "status": "not_configured"}
    argv = shlex.split(configured)
    if not argv:
        return {"available": False, "status": "not_configured"}
    completed = subprocess.run(
        [*argv, str(Path(source).expanduser().resolve())],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "available": True,
        "status": "valid" if completed.returncode == 0 else "invalid",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": argv,
    }
