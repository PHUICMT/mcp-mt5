"""Shared hidden working directory for compile logs and temporary tester ini files."""
from __future__ import annotations

import os
from pathlib import Path


def workdir(source: Path) -> Path:
    """Return the working directory for artefacts related to `source`.

    Resolution order:
      1. `MT5_WORK_DIR` env var (absolute path)
      2. `<source-parent>/.mt5tmp/`

    The directory is created if missing. Add `.mt5tmp/` to `.gitignore` to keep it out of VCS.
    """
    explicit = os.environ.get("MT5_WORK_DIR")
    d = Path(explicit) if explicit else (Path(source).parent / ".mt5tmp")
    d.mkdir(parents=True, exist_ok=True)
    return d
