"""Snapshot source files referenced by a backtest run."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable


def snapshot_sources(sources: Iterable[str | Path], dest: str | Path,
                     label: str | None = None) -> dict:
    """Copy a list of source files into a timestamped snapshot folder.

    Args:
        sources: Iterable of source paths to freeze.
        dest: Parent directory under which snapshots are stored.
        label: Optional human-readable name (defaults to ISO timestamp).
    """
    dest_p = Path(dest)
    dest_p.mkdir(parents=True, exist_ok=True)
    name = label or datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = dest_p / name
    snap_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict] = []
    for s in sources:
        sp = Path(s)
        if not sp.exists():
            continue
        target = snap_dir / sp.name
        # If duplicate basename, prefix with first letter of parent
        if target.exists():
            target = snap_dir / f"{sp.parent.name}__{sp.name}"
        shutil.copy2(sp, target)
        copied.append({"src": str(sp), "snap": str(target), "size": target.stat().st_size})

    manifest = {
        "label": name,
        "created": datetime.now().isoformat(),
        "files": copied,
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"snapshot_dir": str(snap_dir), "file_count": len(copied), "manifest": manifest}


def list_snapshots(dest: str | Path) -> list[dict]:
    dest_p = Path(dest)
    if not dest_p.exists():
        return []
    out: list[dict] = []
    for d in sorted(dest_p.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        manifest = d / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                out.append({"label": d.name, "path": str(d),
                            "file_count": len(data.get("files", [])),
                            "created": data.get("created")})
            except Exception:
                out.append({"label": d.name, "path": str(d), "file_count": None,
                            "created": None, "warning": "manifest unreadable"})
    return out
