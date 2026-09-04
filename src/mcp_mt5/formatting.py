"""MQL formatting via clang-format (treats MQL as C++), with MQL-specific tokens protected."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .parsers import read_text_auto, write_text_preserving


_DEFAULT_STYLE = (
    "{BasedOnStyle: LLVM, IndentWidth: 3, ColumnLimit: 110, "
    "AllowShortFunctionsOnASingleLine: Inline, BreakBeforeBraces: Allman, "
    "PointerAlignment: Left, SortIncludes: false, Language: Cpp}"
)

# clang-format mangles these MQL constructs when it parses them as C++:
#   D'2024.01.01 00:00'  datetime literal      C'255,0,0' / C'0x00FF00'  color literal
#   S'...'               (rare) string literal
#   input group "Risk"   parameter group line  #property ...             MetaEditor directives
_LITERAL_RE = re.compile(r"\b[DCS]'[^'\n]*'")
_LINE_RE = re.compile(r"^[ \t]*(?:input\s+group\b.*|#property\b.*|#resource\b.*|#import\b.*)$", re.MULTILINE)


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


def protect_mql(text: str) -> tuple[str, dict[str, str]]:
    """Replace MQL-only constructs with identifier-like placeholders clang-format leaves alone."""
    table: dict[str, str] = {}

    def _lit(m: re.Match) -> str:
        key = f"__MQLLIT_{len(table)}__"
        table[key] = m.group(0)
        return key

    def _line(m: re.Match) -> str:
        key = f"__MQLLINE_{len(table)}__"
        table[key] = m.group(0)
        return f"// {key}"

    text = _LITERAL_RE.sub(_lit, text)
    text = _LINE_RE.sub(_line, text)
    return text, table


def restore_mql(text: str, table: dict[str, str]) -> str:
    for key, original in table.items():
        if key.startswith("__MQLLINE_"):
            text = re.sub(r"^[ \t]*// " + re.escape(key) + r"[ \t]*$", lambda _m: original, text, flags=re.MULTILINE)
        else:
            text = text.replace(key, original)
    return text


def format_mql(source: str | Path, style: str | None = None, write: bool = False) -> dict:
    """Format an MQL file via `clang-format` (treated as C++).

    Args:
        source: Path to .mq4/.mq5/.mqh.
        style: Optional clang-format style string. Defaults to an MQL-friendly profile.
        write: If True, overwrite the file (keeping its original encoding). Defaults to a dry run.
    """
    p = Path(source)
    if not p.exists():
        return {"error": f"not found: {p}"}
    if not has_clang_format():
        return {"error": "clang-format not found in PATH. Install LLVM or set CLANG_FORMAT_BIN."}

    original = read_text_auto(p)
    protected, table = protect_mql(original)
    rc, stdout, stderr = _run_clang_format(protected, style or _DEFAULT_STYLE)
    if rc != 0:
        return {"error": f"clang-format failed (rc={rc}): {stderr.strip()}"}
    formatted = restore_mql(stdout, table)
    if any(key in formatted for key in table):
        return {"error": "formatter altered a protected MQL token; refusing to write", "file": str(p)}

    changed = formatted != original
    encoding = None
    if write and changed:
        encoding = write_text_preserving(p, formatted)

    return {
        "file": str(p),
        "changed": changed,
        "written": bool(write and changed),
        "encoding": encoding,
        "protected_tokens": len(table),
        "style": style or _DEFAULT_STYLE,
        "size_before": len(original),
        "size_after": len(formatted),
    }


def format_check(source: str | Path, style: str | None = None) -> dict:
    """Report whether a file needs formatting without modifying it."""
    return format_mql(source, style=style, write=False)
