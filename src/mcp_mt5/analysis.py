"""Source analysis: inputs, includes, symbols, metrics, docs, magic numbers."""
from __future__ import annotations

import re
from pathlib import Path

from .parsers import read_text_auto


_INPUT_RE = re.compile(
    r"^\s*(input|sinput|extern)\s+([A-Za-z_][\w:]*)\s+([A-Za-z_]\w*)\s*=\s*([^;]+?)\s*;\s*(?://\s*(.*))?$",
    re.MULTILINE,
)


def extract_inputs(source: str | Path) -> list[dict]:
    """Parse `input <type> <name> = <default>;` declarations from a `.mq4`/`.mq5` source."""
    p = Path(source)
    if not p.exists():
        return []
    text = read_text_auto(p)
    out: list[dict] = []
    for m in _INPUT_RE.finditer(text):
        out.append({
            "kind": m.group(1),
            "type": m.group(2),
            "name": m.group(3),
            "default": m.group(4).strip(),
            "comment": (m.group(5) or "").strip(),
        })
    return out


_TF_ENUM_TO_CODE = {
    "PERIOD_M1": 1, "PERIOD_M2": 2, "PERIOD_M3": 3, "PERIOD_M4": 4, "PERIOD_M5": 5,
    "PERIOD_M6": 6, "PERIOD_M10": 10, "PERIOD_M12": 12, "PERIOD_M15": 15, "PERIOD_M20": 20,
    "PERIOD_M30": 30, "PERIOD_H1": 16385, "PERIOD_H2": 16386, "PERIOD_H3": 16387,
    "PERIOD_H4": 16388, "PERIOD_H6": 16390, "PERIOD_H8": 16392, "PERIOD_H12": 16396,
    "PERIOD_D1": 16408, "PERIOD_W1": 32769, "PERIOD_MN1": 49153,
}


def gen_tester_inputs(source: str | Path) -> str:
    """Build a `[TesterInputs]` block from EA input declarations."""
    inputs = extract_inputs(source)
    if not inputs:
        return ""
    lines = ["[TesterInputs]"]
    for inp in inputs:
        default = inp["default"]
        if default in _TF_ENUM_TO_CODE:
            default = str(_TF_ENUM_TO_CODE[default])
        if default.lower() in ("true", "false"):
            default = default.lower()
        # Strip surrounding quotes only for the value column
        clean = default.strip().strip('"')
        lines.append(f"{inp['name']}={clean}||{clean}||0||{clean}||N")
    return "\n".join(lines) + "\n"


_INCLUDE_QUOTE_RE = re.compile(r'^\s*#include\s+"([^"]+)"', re.MULTILINE)
_INCLUDE_ANGLE_RE = re.compile(r"^\s*#include\s+<([^>]+)>", re.MULTILINE)


def resolve_includes(source: str | Path, mql_root: str | Path | None = None,
                     visited: set[str] | None = None) -> dict:
    """Recursively resolve `#include` directives.

    Returns:
        {"file": <path>, "missing": [...], "resolved": [...children dicts...]}
    """
    p = Path(source).resolve()
    visited = visited or set()
    if str(p) in visited:
        return {"file": str(p), "cycle": True, "resolved": [], "missing": []}
    visited.add(str(p))

    if not p.exists():
        return {"file": str(p), "exists": False, "resolved": [], "missing": []}

    text = read_text_auto(p)
    resolved: list[dict] = []
    missing: list[str] = []

    for m in _INCLUDE_QUOTE_RE.finditer(text):
        rel = m.group(1).replace("\\", "/")
        target = (p.parent / rel).resolve()
        if target.exists():
            resolved.append(resolve_includes(target, mql_root, visited))
        else:
            missing.append(rel)

    if mql_root:
        root = Path(mql_root)
        for m in _INCLUDE_ANGLE_RE.finditer(text):
            rel = m.group(1).replace("\\", "/")
            target = root / "Include" / rel
            if target.exists():
                resolved.append(resolve_includes(target, mql_root, visited))
            else:
                missing.append(f"<{rel}>")

    return {
        "file": str(p),
        "exists": True,
        "resolved": resolved,
        "missing": missing,
    }


def _strip_comments_strings(text: str) -> str:
    """Remove `//`, `/* */` comments and string/char literals to reduce false matches."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
    return text


def find_symbol(symbol: str, root: str | Path, exts: tuple[str, ...] = (".mq4", ".mq5", ".mqh"),
                limit: int = 200) -> list[dict]:
    """Grep a symbol in MQL files, skipping comments and string literals."""
    pat = re.compile(r"\b" + re.escape(symbol) + r"\b")
    out: list[dict] = []
    root_p = Path(root)
    if not root_p.exists():
        return out
    for ext in exts:
        for f in root_p.rglob(f"*{ext}"):
            try:
                text = read_text_auto(f)
            except Exception:
                continue
            cleaned = _strip_comments_strings(text)
            for i, line in enumerate(cleaned.splitlines(), 1):
                if pat.search(line):
                    raw_line = text.splitlines()[i - 1] if i - 1 < len(text.splitlines()) else ""
                    out.append({"file": str(f), "line": i, "text": raw_line.strip()})
                    if len(out) >= limit:
                        return out
    return out


_FUNC_RE = re.compile(
    r"^\s*(?:[A-Za-z_][\w:]*\s+){1,3}([A-Za-z_]\w*)\s*\([^;]*?\)\s*\{",
    re.MULTILINE,
)


def code_metrics(source: str | Path) -> dict:
    """Compute LOC, function count, max nesting, and file size for a single file."""
    p = Path(source)
    if not p.exists():
        return {"error": f"not found: {p}"}
    text = read_text_auto(p)
    lines = text.splitlines()

    code_lines = 0
    blank_lines = 0
    comment_lines = 0
    in_block_comment = False
    for ln in lines:
        s = ln.strip()
        if not s:
            blank_lines += 1
            continue
        if in_block_comment:
            comment_lines += 1
            if "*/" in s:
                in_block_comment = False
            continue
        if s.startswith("/*"):
            comment_lines += 1
            if "*/" not in s:
                in_block_comment = True
            continue
        if s.startswith("//"):
            comment_lines += 1
            continue
        code_lines += 1

    cleaned = _strip_comments_strings(text)
    nesting = max_nesting = 0
    for ch in cleaned:
        if ch == "{":
            nesting += 1
            max_nesting = max(max_nesting, nesting)
        elif ch == "}":
            nesting = max(0, nesting - 1)

    func_count = len(_FUNC_RE.findall(cleaned))

    return {
        "file": str(p),
        "size_bytes": p.stat().st_size,
        "total_lines": len(lines),
        "code_lines": code_lines,
        "comment_lines": comment_lines,
        "blank_lines": blank_lines,
        "function_count": func_count,
        "max_nesting": max_nesting,
    }


_DOC_BLOCK_RE = re.compile(
    r"//\+-+\+\s*\n((?://\|.*\n)+)//\+-+\+",
)


def extract_doc(source: str | Path) -> list[dict]:
    """Pull MetaEditor `//+-+ //| ... +-+` doc blocks from a source file."""
    p = Path(source)
    if not p.exists():
        return []
    text = read_text_auto(p)
    out: list[dict] = []
    for m in _DOC_BLOCK_RE.finditer(text):
        body = m.group(1)
        cleaned = "\n".join(re.sub(r"^//\|\s?", "", ln).rstrip() for ln in body.splitlines())
        line_no = text[: m.start()].count("\n") + 1
        out.append({"line": line_no, "text": cleaned.strip()})
    return out


_MAGIC_INT_RE = re.compile(r"\b(\d{4,})\b")


def find_magic_collision(root: str | Path, var_pattern: str = "Magic",
                         exts: tuple[str, ...] = (".mq4", ".mq5", ".mqh")) -> dict:
    """Detect duplicate magic-number assignments and bare 4+digit literals.

    Returns {"assignments": {value: [{file, line}]}, "literals": [...]}
    """
    root_p = Path(root)
    assignments: dict[str, list[dict]] = {}
    assign_re = re.compile(
        rf"\b\w*{re.escape(var_pattern)}\w*\s*=\s*(\d+)\s*;",
        re.IGNORECASE,
    )
    for ext in exts:
        for f in root_p.rglob(f"*{ext}"):
            try:
                text = read_text_auto(f)
            except Exception:
                continue
            cleaned = _strip_comments_strings(text)
            for i, line in enumerate(cleaned.splitlines(), 1):
                m = assign_re.search(line)
                if m:
                    val = m.group(1)
                    assignments.setdefault(val, []).append({"file": str(f), "line": i})
    duplicates = {v: locs for v, locs in assignments.items() if len(locs) > 1}
    return {"assignments": assignments, "duplicates": duplicates}


def list_mql_files(root: str | Path, exts: tuple[str, ...] = (".mq4", ".mq5", ".mqh")) -> list[str]:
    root_p = Path(root)
    if not root_p.exists():
        return []
    out: list[str] = []
    for ext in exts:
        out.extend(str(f) for f in root_p.rglob(f"*{ext}"))
    return sorted(out)


def aggregate_metrics(root: str | Path) -> dict:
    """Run code_metrics across all MQL files under root and aggregate."""
    files = list_mql_files(root)
    per_file: list[dict] = []
    totals = {"total_lines": 0, "code_lines": 0, "comment_lines": 0, "blank_lines": 0,
              "function_count": 0, "size_bytes": 0}
    for f in files:
        m = code_metrics(f)
        if "error" in m:
            continue
        per_file.append(m)
        for k in totals:
            totals[k] += m.get(k, 0) or 0
    return {"file_count": len(per_file), "totals": totals, "per_file": per_file[:200]}
