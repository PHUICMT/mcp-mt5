"""Project-wide refactor helpers."""
from __future__ import annotations

import re
from pathlib import Path

from .parsers import read_text_auto


def rename_symbol(old: str, new: str, root: str | Path,
                  exts: tuple[str, ...] = (".mq4", ".mq5", ".mqh"),
                  dry_run: bool = True) -> dict:
    """Rename `old` → `new` across all MQL files (whole-word match)."""
    pat = re.compile(r"\b" + re.escape(old) + r"\b")
    root_p = Path(root)
    if not root_p.exists():
        return {"error": f"root not found: {root_p}"}

    changes: list[dict] = []
    for ext in exts:
        for f in root_p.rglob(f"*{ext}"):
            try:
                text = read_text_auto(f)
            except Exception:
                continue
            new_text, count = pat.subn(new, text)
            if count == 0:
                continue
            changes.append({"file": str(f), "replacements": count})
            if not dry_run:
                f.write_text(new_text, encoding="utf-8")
    return {
        "old": old,
        "new": new,
        "files_changed": len(changes),
        "total_replacements": sum(c["replacements"] for c in changes),
        "changes": changes,
        "dry_run": dry_run,
    }
