from __future__ import annotations

import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def remote_key_filename(root: Path = ROOT) -> str | None:
    raw = (os.getenv("REMOTE_KEY_FILENAME") or os.getenv("REMOTE_KEY") or "").strip()
    if not raw:
        return None
    expanded = Path(os.path.expandvars(raw)).expanduser()
    if not expanded.is_absolute():
        expanded = root / expanded
    return str(expanded.resolve())


def ssh_connect_kwargs(password: str | None, root: Path = ROOT) -> dict[str, Any]:
    return {
        "password": password,
        "key_filename": remote_key_filename(root),
        "look_for_keys": False,
        "allow_agent": False,
    }
