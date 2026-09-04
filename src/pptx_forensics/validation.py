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
import math
from typing import Any


def validate_with_openxml_sdk(source: str | Path, command: str | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    """Run an optional Open XML SDK validator sidecar.

    ``command`` may be supplied directly or through
    ``OPENXML_VALIDATOR_COMMAND``. The source path is appended as the final
    argument. The sidecar should return exit code 0 for a valid package.
    """
    configured = command if command is not None else os.environ.get("OPENXML_VALIDATOR_COMMAND")
    if not configured:
        return {"available": False, "status": "not_configured"}
    try:
        argv = shlex.split(configured)
    except ValueError as exc:
        return {"available": False, "status": "invalid_configuration", "error": str(exc)}
    if not argv:
        return {"available": False, "status": "not_configured"}
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0:
        return {"available": False, "status": "invalid_configuration", "error": "timeout must be finite and positive", "command": argv}
    try:
        completed = subprocess.run(
            [*argv, str(Path(source).expanduser().resolve())],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "available": True,
            "status": "timeout",
            "error": str(exc),
            "command": argv,
        }
    except (OSError, UnicodeError) as exc:
        return {
            "available": False,
            "status": "unavailable",
            "error": str(exc),
            "command": argv,
        }
    return {
        "available": True,
        "status": "valid" if completed.returncode == 0 else "invalid",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": argv,
    }
