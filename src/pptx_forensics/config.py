"""Small local configuration helpers."""

from __future__ import annotations

import os
from pathlib import Path
import re


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(path: str | Path | None = None) -> Path | None:
    """Load a simple project ``.env`` file without overriding real env vars."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path).expanduser().resolve())
    candidates.extend(
        [
            Path.cwd() / ".env",
            Path(__file__).resolve().parents[2] / ".env",
        ]
    )
    seen: set[Path] = set()
    dotenv_path = next(
        (candidate for candidate in candidates if not (candidate in seen or seen.add(candidate)) and candidate.is_file()),
        None,
    )
    if dotenv_path is None:
        return None
    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = (item.strip() for item in line.split("=", 1))
        if not _ENV_KEY.fullmatch(key) or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value
    return dotenv_path
