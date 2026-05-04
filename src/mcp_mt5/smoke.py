"""Smoke test: compile + 1-day backtest + journal error scan."""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .parsers import read_text_auto


_RUNTIME_ERROR_PATTERNS = [
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"\bcritical\b", re.IGNORECASE),
    re.compile(r"\bfatal\b", re.IGNORECASE),
    re.compile(r"OnInit\s+returned\s+(?!INIT_SUCCEEDED)", re.IGNORECASE),
    re.compile(r"unable to load", re.IGNORECASE),
    re.compile(r"failed to (initialize|run)", re.IGNORECASE),
    re.compile(r"access violation", re.IGNORECASE),
    re.compile(r"divide by zero", re.IGNORECASE),
    re.compile(r"array out of range", re.IGNORECASE),
    re.compile(r"stack overflow", re.IGNORECASE),
]

# Lines we explicitly do not want to count as failures.
_BENIGN_PATTERNS = [
    re.compile(r"\b0\s+errors?\b", re.IGNORECASE),
    re.compile(r"no errors", re.IGNORECASE),
    re.compile(r"successfully", re.IGNORECASE),
]


def write_smoke_tester_ini(
    expert_name: str,
    target_path: Path,
    symbol: str = "EURUSD",
    period: str = "M15",
    days: int = 1,
    deposit: float = 10000.0,
    leverage: int = 500,
) -> Path:
    """Write a minimal headless tester.ini for a smoke run."""
    end = datetime.now().date() - timedelta(days=2)
    start = end - timedelta(days=days)
    body = f"""\
[Tester]
Expert={expert_name}
Symbol={symbol}
Period={period}
Optimization=0
Model=2
FromDate={start.strftime('%Y.%m.%d')}
ToDate={end.strftime('%Y.%m.%d')}
ForwardMode=0
Deposit={deposit}
Currency=USD
Leverage={leverage}
ExecutionMode=0
Visual=0
ShutdownTerminal=1
Report=smoke_report

[TesterInputs]
"""
    target_path.write_text(body, encoding="utf-8")
    return target_path


def scan_journal_for_errors(log_path: Path) -> dict:
    """Read a tester journal and return matched error lines (with benign lines filtered)."""
    if not log_path.exists():
        return {"error": f"log not found: {log_path}", "matches": []}
    text = read_text_auto(log_path)
    matches: list[dict] = []
    for i, line in enumerate(text.splitlines(), 1):
        if any(p.search(line) for p in _BENIGN_PATTERNS):
            continue
        for pat in _RUNTIME_ERROR_PATTERNS:
            if pat.search(line):
                matches.append({"line": i, "text": line.strip(), "rule": pat.pattern})
                break
    return {"matches": matches, "match_count": len(matches), "log_path": str(log_path)}


def run_smoke(
    layout,
    source: str | Path,
    expert_name: Optional[str] = None,
    symbol: str = "EURUSD",
    period: str = "M15",
    days: int = 1,
    timeout_sec: int = 600,
) -> dict:
    """End-to-end smoke harness.

    1. Compile the source.
    2. Deploy the binary.
    3. Write a 1-day headless `tester.ini`.
    4. Launch the terminal and wait for shutdown.
    5. Scan the resulting tester journal for runtime errors.

    Returns a `pass`/`fail` summary plus the full sub-results so the caller can drill in.
    """
    src = Path(source)
    if not src.exists():
        return {"ok": False, "stage": "input", "error": f"source not found: {src}"}

    # 1. Compile
    log_path = src.with_suffix(src.suffix + ".log")
    cmd = [str(layout.metaeditor), f"/compile:{src}", f"/include:{layout.mql_root}",
           f"/log:{log_path}"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "compile", "error": "compile timeout"}

    if log_path.exists():
        compile_text = read_text_auto(log_path)
        if "0 errors" not in compile_text and "Result: 0 errors" not in compile_text:
            tail = "\n".join(compile_text.splitlines()[-30:])
            return {"ok": False, "stage": "compile", "log_excerpt": tail}

    # 2. Deploy
    ext = ".ex5" if layout.edition == "mt5" else ".ex4"
    binary = src.with_suffix(ext)
    if not binary.exists():
        return {"ok": False, "stage": "compile", "error": f"binary missing: {binary}"}
    target = layout.experts_dir / binary.name
    target.write_bytes(binary.read_bytes())

    # 3. tester.ini
    ea_name = expert_name or binary.stem
    cfg = src.with_name(f"{src.stem}.smoke.ini")
    write_smoke_tester_ini(ea_name, cfg, symbol=symbol, period=period, days=days)

    # 4. Run terminal
    start = time.time()
    try:
        subprocess.run([str(layout.terminal), f"/config:{cfg}"], timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "backtest", "error": f"terminal timeout after {timeout_sec}s"}
    elapsed = round(time.time() - start, 2)

    # 5. Scan journal
    journal = None
    if layout.tester_logs.exists():
        logs = sorted(layout.tester_logs.glob("*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            journal = logs[0]

    if not journal:
        return {"ok": False, "stage": "journal", "error": "no tester log produced",
                "elapsed_sec": elapsed}

    scan = scan_journal_for_errors(journal)
    return {
        "ok": scan["match_count"] == 0,
        "stage": "complete",
        "elapsed_sec": elapsed,
        "expert": ea_name,
        "symbol": symbol,
        "tester_log": str(journal),
        "errors_found": scan["match_count"],
        "errors_sample": scan["matches"][:20],
    }
