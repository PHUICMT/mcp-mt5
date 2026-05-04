"""Brace-aware refactor helpers (no full AST — uses brace counting + comment-aware scanning)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .parsers import read_text_auto
from .analysis import _strip_comments_strings


_PARAM_HINT_RE = re.compile(r"\b(int|long|double|float|bool|string|datetime|color|char|uchar|short|ushort|uint|ulong)\s+(\w+)")


def _enclosing_function(text: str, line_start: int) -> Optional[dict]:
    """Find the function whose body contains `line_start` (1-based)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    func_re = re.compile(
        r"^[ \t]*(?:[A-Za-z_][\w:]*\s+){1,3}([A-Za-z_]\w*)\s*\(([^;]*?)\)\s*\{",
        re.MULTILINE,
    )
    cleaned = _strip_comments_strings(text)
    lines = text.splitlines()
    if line_start < 1 or line_start > len(lines):
        return None

    target_offset = sum(len(lines[i]) + 1 for i in range(line_start - 1))

    candidates: list[dict] = []
    for m in func_re.finditer(cleaned):
        body_start = m.end()
        depth = 1
        i = body_start
        while i < len(cleaned) and depth > 0:
            ch = cleaned[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth == 0:
            body_end = i
            if body_start <= target_offset <= body_end:
                # Convert offset back to line numbers
                func_line_start = cleaned[:m.start()].count("\n") + 1
                func_line_end = cleaned[:body_end].count("\n") + 1
                candidates.append({
                    "name": m.group(1),
                    "params": m.group(2).strip(),
                    "line_start": func_line_start,
                    "line_end": func_line_end,
                    "body_start_offset": body_start,
                    "body_end_offset": body_end,
                })
    return candidates[-1] if candidates else None


def _gather_referenced_locals(block_text: str, source_above: str) -> list[str]:
    """Identifiers used in block_text that are also declared above as typed locals."""
    cleaned_block = _strip_comments_strings(block_text)
    used = set(re.findall(r"\b([A-Za-z_]\w*)\b", cleaned_block))
    cleaned_above = _strip_comments_strings(source_above)
    declared: dict[str, str] = {}
    for m in _PARAM_HINT_RE.finditer(cleaned_above):
        declared[m.group(2)] = m.group(1)
    return [f"{declared[name]} {name}" for name in used if name in declared]


def extract_function(
    source: str | Path,
    line_start: int,
    line_end: int,
    new_name: str,
    return_type: str = "void",
    target_file: Optional[str | Path] = None,
    dry_run: bool = True,
) -> dict:
    """Extract `lines[line_start..line_end]` (inclusive) into a new helper function.

    If `target_file` is given, append the new function there with an `#include`-friendly
    signature. Otherwise insert it directly above the enclosing function in the same file.

    Limitations
    -----------
    Uses brace counting + regex parameter detection — not a real AST. Works well on
    contiguous, statement-aligned block extractions; manual review still recommended.
    """
    src = Path(source)
    if not src.exists():
        return {"error": f"not found: {src}"}
    text = read_text_auto(src).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()

    if not (1 <= line_start <= line_end <= len(lines)):
        return {"error": f"bad line range {line_start}..{line_end} (file has {len(lines)} lines)"}

    enclosing = _enclosing_function(text, line_start)
    if not enclosing:
        return {"error": f"line {line_start} is not inside a function body"}

    block = "\n".join(lines[line_start - 1 : line_end])
    indent_match = re.match(r"^[ \t]*", lines[line_start - 1])
    indent = indent_match.group(0) if indent_match else ""

    above_text = "\n".join(lines[: line_start - 1])
    typed_params = _gather_referenced_locals(block, above_text)
    typed_params = sorted(set(typed_params))[:10]
    param_list = ", ".join(typed_params)
    arg_list = ", ".join(p.split()[-1] for p in typed_params)

    helper_lines = [
        f"{return_type} {new_name}({param_list})",
        "{",
        block,
        "}",
        "",
    ]
    helper_block = "\n".join(helper_lines)

    call_site = f"{indent}{new_name}({arg_list});"
    new_lines = lines[: line_start - 1] + [call_site] + lines[line_end:]

    if target_file:
        target = Path(target_file)
        if not dry_run:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            target.write_text(existing.rstrip() + "\n\n" + helper_block, encoding="utf-8")
            src.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return {
            "mode": "external",
            "target_file": str(target),
            "helper": helper_block,
            "call_site": call_site,
            "params": typed_params,
            "dry_run": dry_run,
        }

    # Insert helper above the enclosing function in the same file
    insert_at = enclosing["line_start"] - 1
    out = new_lines[:insert_at] + helper_block.splitlines() + new_lines[insert_at:]

    if not dry_run:
        src.write_text("\n".join(out) + "\n", encoding="utf-8")

    return {
        "mode": "inline",
        "enclosing_function": enclosing["name"],
        "helper": helper_block,
        "call_site": call_site,
        "params": typed_params,
        "preview": "\n".join(out[: max(0, insert_at - 2)] + [
            "...",
            "/* helper inserted here */",
            *helper_block.splitlines(),
            "...",
        ]),
        "dry_run": dry_run,
    }
