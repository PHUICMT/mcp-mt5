"""MQL formatting via clang-format (treats MQL as C++)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .parsers import read_text_auto


_DEFAULT_STYLE = (
    "{BasedOnStyle: LLVM, IndentWidth: 3, ColumnLimit: 110, "
    "AllowShortFunctionsOnASingleLine: Inline, BreakBeforeBraces: Allman, "
    "PointerAlignment: Left, SortIncludes: false, Language: Cpp}"
)


def has_clang_format() -> bool:
    return shutil.which("clang-format") is not None


def _run_clang_format(text: str, style: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["clang-format", f"-style={style}", "-assume-filename=source.cpp"],
        input=text,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def format_mql(source: str | Path, style: str | None = None, write: bool = True) -> dict:
    """Format an MQL file via `clang-format` (treated as C++).

    Args:
        source: Path to .mq4/.mq5/.mqh.
        style: Optional clang-format style string (YAML-flow or named style). Defaults to a sensible MQL-friendly profile.
        write: If True, overwrite the file with formatted output. If False, return the diff only.
    """
    p = Path(source)
    if not p.exists():
        return {"error": f"not found: {p}"}
    if not has_clang_format():
        return {"error": "clang-format not found in PATH. Install LLVM or set CLANG_FORMAT_BIN."}

    original = read_text_auto(p)
    rc, stdout, stderr = _run_clang_format(original, style or _DEFAULT_STYLE)
    if rc != 0:
        return {"error": f"clang-format failed (rc={rc}): {stderr.strip()}"}

    changed = stdout != original
    if write and changed:
        p.write_text(stdout, encoding="utf-8")

    return {
        "file": str(p),
        "changed": changed,
        "written": write and changed,
        "style": style or _DEFAULT_STYLE,
        "size_before": len(original),
        "size_after": len(stdout),
    }


def format_check(source: str | Path, style: str | None = None) -> dict:
    """Report whether a file needs formatting without modifying it."""
    return format_mql(source, style=style, write=False)
