"""Custom MQL lint rules + tester.ini validation."""
from __future__ import annotations

import re
from pathlib import Path

from .parsers import read_text_auto
from .analysis import extract_inputs, _strip_comments_strings


# Functions that signal MT4-style trading code in an MT5 source — these
# still compile in MT5 but are deprecated; CTrade is the modern path.
_DEPRECATED_FUNCS = {
    "OrderSend": "Use CTrade::Buy/Sell/PositionOpen instead",
    "OrderClose": "Use CTrade::PositionClose",
    "OrderModify": "Use CTrade::PositionModify",
    "OrderSelect": "Use PositionGetTicket / HistorySelectByPosition",
    "OrderType": "Use PositionGetInteger(POSITION_TYPE)",
    "OrderLots": "Use PositionGetDouble(POSITION_VOLUME)",
    "OrderSymbol": "Use PositionGetString(POSITION_SYMBOL)",
    "OrderMagicNumber": "Use PositionGetInteger(POSITION_MAGIC)",
    "AccountBalance": "Use AccountInfoDouble(ACCOUNT_BALANCE)",
    "AccountEquity": "Use AccountInfoDouble(ACCOUNT_EQUITY)",
    "Bars": "Use iBars or Bars(_Symbol, _Period)",
    "Ask": "Use SymbolInfoDouble(_Symbol, SYMBOL_ASK)",
    "Bid": "Use SymbolInfoDouble(_Symbol, SYMBOL_BID)",
}


def check_deprecated(source: str | Path) -> list[dict]:
    """Flag deprecated MT4-style API calls in an MT5 source file."""
    p = Path(source)
    if not p.exists():
        return []
    text = read_text_auto(p)
    cleaned = _strip_comments_strings(text)
    # Ask/Bid are MT4 predefined variables (no parens). Treat them specially.
    bare_vars = {"Ask", "Bid"}
    findings: list[dict] = []
    for fn, suggestion in _DEPRECATED_FUNCS.items():
        if fn in bare_vars:
            pat = re.compile(rf"\b{re.escape(fn)}\b(?!\s*\w)")
        else:
            pat = re.compile(rf"\b{re.escape(fn)}\b\s*\(")
        for i, line in enumerate(cleaned.splitlines(), 1):
            if pat.search(line):
                findings.append({"line": i, "func": fn, "suggestion": suggestion})
    return findings


def lint_basic(source: str | Path) -> dict:
    """Run a small set of structural lints over a single source file."""
    p = Path(source)
    if not p.exists():
        return {"error": f"not found: {p}"}
    text = read_text_auto(p)
    cleaned = _strip_comments_strings(text)
    findings: list[dict] = []

    is_ea = p.suffix.lower() in (".mq4", ".mq5")

    if is_ea:
        if not re.search(r"\bvoid\s+OnTick\s*\(\s*\)", cleaned) and not re.search(r"\bvoid\s+OnStart\s*\(\s*\)", cleaned):
            findings.append({"rule": "missing_entry_point",
                             "message": "EA source has no OnTick() or OnStart() handler"})
        if not re.search(r"\b(int|void)\s+OnInit\s*\(\s*\)", cleaned):
            findings.append({"rule": "missing_oninit",
                             "message": "EA source has no OnInit() handler"})
        if not re.search(r"\bvoid\s+OnDeinit\s*\(\s*const\s+int", cleaned):
            findings.append({"rule": "missing_ondeinit",
                             "message": "EA source has no OnDeinit() handler"})

    # Hardcoded magic literal in OrderSend / trade.PositionOpen calls
    for i, line in enumerate(cleaned.splitlines(), 1):
        if re.search(r"\.(?:Buy|Sell|PositionOpen)\s*\([^)]*?,\s*\d{4,}\s*[,)]", line):
            findings.append({"rule": "hardcoded_magic",
                             "line": i,
                             "message": "Hardcoded magic number in trade call — use an Inp* input"})

    # Unused inputs (declared but never referenced)
    inputs = extract_inputs(p)
    decl_names = {i["name"] for i in inputs}
    used: set[str] = set()
    for name in decl_names:
        # one decl + at least one use
        count = len(re.findall(rf"\b{re.escape(name)}\b", cleaned))
        if count >= 2:
            used.add(name)
    for name in decl_names - used:
        findings.append({"rule": "unused_input", "name": name,
                         "message": f"Input '{name}' declared but never referenced"})

    # Hardcoded symbol literal in trade calls
    for i, line in enumerate(cleaned.splitlines(), 1):
        if re.search(r'(?:Buy|Sell|PositionOpen|SymbolInfo\w+)\s*\(\s*"[A-Z]{6,8}"', line):
            findings.append({"rule": "hardcoded_symbol",
                             "line": i,
                             "message": "Hardcoded symbol literal — prefer InpSymbol or _Symbol"})

    return {"file": str(p), "findings": findings, "issue_count": len(findings)}


def validate_tester_ini(config: str | Path, source: str | Path | None = None) -> dict:
    """Sanity-check a tester.ini file. If `source` given, also cross-check inputs vs EA."""
    p = Path(config)
    if not p.exists():
        return {"error": f"not found: {p}"}
    text = read_text_auto(p)
    issues: list[dict] = []

    sections: dict[str, dict[str, str]] = {}
    current = ""
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith(";") or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1]
            sections.setdefault(current, {})
            continue
        if "=" in s and current:
            k, v = s.split("=", 1)
            sections.setdefault(current, {})[k.strip()] = v.strip()

    tester = sections.get("Tester", {})
    required = ["Expert", "Symbol", "Period", "FromDate", "ToDate", "Deposit"]
    for key in required:
        if key not in tester:
            issues.append({"section": "Tester", "key": key, "message": "missing required key"})

    # Date format check
    date_re = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
    for key in ("FromDate", "ToDate"):
        if key in tester and not date_re.match(tester[key]):
            issues.append({"section": "Tester", "key": key,
                           "message": f"invalid date format '{tester[key]}', expected YYYY.MM.DD"})

    # Numeric sanity
    for key in ("Deposit", "Leverage"):
        if key in tester:
            try:
                if float(tester[key]) <= 0:
                    issues.append({"section": "Tester", "key": key, "message": "must be > 0"})
            except ValueError:
                issues.append({"section": "Tester", "key": key,
                               "message": f"non-numeric value '{tester[key]}'"})

    if "ShutdownTerminal" in tester and tester["ShutdownTerminal"] not in ("0", "1"):
        issues.append({"section": "Tester", "key": "ShutdownTerminal",
                       "message": "must be 0 or 1"})

    if source:
        ea_inputs = {i["name"] for i in extract_inputs(source)}
        ini_inputs = set(sections.get("TesterInputs", {}).keys())
        for unknown in ini_inputs - ea_inputs:
            issues.append({"section": "TesterInputs", "key": unknown,
                           "message": f"input '{unknown}' not declared in EA source"})
        # Optional: warn about declared inputs missing from ini (informational)
        for missing in ea_inputs - ini_inputs:
            issues.append({"section": "TesterInputs", "key": missing,
                           "message": f"EA input '{missing}' has no entry in [TesterInputs] (will use source default)",
                           "severity": "info"})

    return {
        "config": str(p),
        "sections_found": list(sections.keys()),
        "issues": issues,
        "issue_count": sum(1 for i in issues if i.get("severity") != "info"),
    }
