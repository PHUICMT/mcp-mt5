"""MCP server wrapping MetaTrader 4/5 build pipeline (compile, deploy, backtest, logs)."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .paths import detect_layout, MT5Layout
from .parsers import (
    parse_compile_log,
    parse_tester_report,
    read_text_auto,
    iter_journal_lines,
)

mcp = FastMCP("mt5")

# Layout resolved lazily so tests can override env first
_layout_cache: Optional[MT5Layout] = None


def layout() -> MT5Layout:
    global _layout_cache
    if _layout_cache is None:
        _layout_cache = detect_layout()
    return _layout_cache


@mcp.tool()
def env_info() -> dict:
    """Resolve and report MT4/5 paths, terminal hash, and missing-component issues."""
    L = layout()
    return {
        "edition": L.edition,
        "install": str(L.install),
        "data": str(L.data),
        "terminal_hash": L.terminal_hash,
        "metaeditor": str(L.metaeditor),
        "terminal": str(L.terminal),
        "mql_root": str(L.mql_root),
        "include_dir": str(L.include_dir),
        "experts_dir": str(L.experts_dir),
        "files_dir": str(L.files_dir),
        "logs_dir": str(L.logs_dir),
        "tester_dir": str(L.tester_dir),
        "issues": L.issues(),
    }


@mcp.tool()
def list_terminals() -> dict:
    """Enumerate all MetaTrader terminal data folders under %APPDATA%\\MetaQuotes\\Terminal."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return {"error": "APPDATA env not set"}
    base = Path(appdata) / "MetaQuotes" / "Terminal"
    if not base.exists():
        return {"error": f"missing: {base}"}

    terminals = []
    for d in base.iterdir():
        if not d.is_dir() or len(d.name) != 32:
            continue
        origin_file = d / "origin.txt"
        origin = None
        if origin_file.exists():
            try:
                raw = origin_file.read_bytes()
                origin = (raw.decode("utf-16", errors="replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                          else raw.decode("utf-8", errors="replace")).strip()
            except Exception:
                pass
        terminals.append({
            "hash": d.name,
            "origin": origin,
            "data_dir": str(d),
        })
    return {"count": len(terminals), "terminals": terminals}


@mcp.tool()
def compile(
    source: str,
    include: Optional[str] = None,
    log_file: Optional[str] = None,
    timeout_sec: int = 300,
) -> dict:
    """Compile a .mq4/.mq5/.mqh source via MetaEditor CLI.

    Args:
        source: Absolute path to the source file.
        include: Optional MQL root override (parent of `Include/`). Defaults to terminal MQL root.
        log_file: Optional explicit log path. Defaults to <source>.log.
        timeout_sec: Subprocess timeout.

    Returns: returncode, structured `errors`/`warnings` lists, `result_errors`/`result_warnings`,
             `log_path`, `log_excerpt` (last 80 lines), `cmd`.
    """
    L = layout()
    src = Path(source)
    if not src.exists():
        return {"error": f"source not found: {src}"}
    if not L.metaeditor.exists():
        return {"error": f"MetaEditor missing: {L.metaeditor}"}

    inc = Path(include) if include else L.mql_root
    log_path = Path(log_file) if log_file else src.with_suffix(src.suffix + ".log")

    cmd = [
        str(L.metaeditor),
        f"/compile:{src}",
        f"/include:{inc}",
        f"/log:{log_path}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        rc = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        rc = -1
        stdout = ""
        stderr = f"timeout after {timeout_sec}s: {e}"

    parsed = {"errors": [], "warnings": [], "result_errors": None, "result_warnings": None, "ok": False}
    excerpt = ""
    if log_path.exists():
        try:
            text = read_text_auto(log_path)
            parsed = parse_compile_log(text)
            excerpt = "\n".join(text.splitlines()[-80:])
        except Exception as e:
            excerpt = f"(log read failed: {e})"

    return {
        "returncode": rc,
        "cmd": " ".join(cmd),
        "log_path": str(log_path),
        "ok": parsed["ok"],
        "result_errors": parsed["result_errors"],
        "result_warnings": parsed["result_warnings"],
        "errors": parsed["errors"][:50],
        "warnings": parsed["warnings"][:50],
        "log_excerpt": excerpt,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


@mcp.tool()
def run_backtest(
    config: str,
    wait: bool = True,
    timeout_sec: int = 1800,
    portable: bool = False,
) -> dict:
    """Launch terminal with /config:<tester.ini>.

    Args:
        config: Absolute path to tester.ini.
        wait: Block until terminal exits (requires `ShutdownTerminal=1` in ini).
        timeout_sec: Wait timeout.
        portable: Pass /portable flag.

    Returns: returncode, elapsed_sec, latest_tester_log path.
    """
    L = layout()
    cfg = Path(config)
    if not cfg.exists():
        return {"error": f"config not found: {cfg}"}
    if not L.terminal.exists():
        return {"error": f"terminal missing: {L.terminal}"}

    cmd = [str(L.terminal), f"/config:{cfg}"]
    if portable:
        cmd.append("/portable")

    start = time.time()
    rc: Optional[int] = None
    if wait:
        try:
            proc = subprocess.run(cmd, timeout=timeout_sec)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = -1
    else:
        subprocess.Popen(cmd)

    elapsed = round(time.time() - start, 2)
    latest_log = None
    if L.tester_logs.exists():
        logs = sorted(L.tester_logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            latest_log = str(logs[0])

    return {
        "returncode": rc,
        "elapsed_sec": elapsed,
        "cmd": " ".join(cmd),
        "latest_tester_log": latest_log,
    }


@mcp.tool()
def kill_terminal() -> dict:
    """Force-kill all running terminal processes for the configured edition."""
    L = layout()
    target = L.terminal.name
    try:
        proc = subprocess.run(["taskkill", "/F", "/IM", target], capture_output=True, text=True)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def tail_log(mode: str = "live", lines: int = 100, date: Optional[str] = None,
             structured: bool = False) -> dict:
    """Read last N lines from terminal logs.

    Args:
        mode: "live" (Files/LiveLog.txt), "journal" (Logs/YYYYMMDD.log), "tester" (latest tester log).
        lines: Tail line count.
        date: Override YYYYMMDD for journal mode.
        structured: Parse journal lines into ts/source/message records.
    """
    L = layout()
    if mode == "live":
        path = L.files_dir / "LiveLog.txt"
    elif mode == "journal":
        d = date or datetime.now().strftime("%Y%m%d")
        path = L.logs_dir / f"{d}.log"
    elif mode == "tester":
        if not L.tester_logs.exists():
            return {"error": f"tester logs dir missing: {L.tester_logs}"}
        files = sorted(L.tester_logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return {"error": "no tester logs"}
        path = files[0]
    else:
        return {"error": f"unknown mode: {mode}"}

    if not path.exists():
        return {"error": f"log not found: {path}", "path": str(path)}

    text = read_text_auto(path)
    tail_lines = text.splitlines()[-lines:]
    out: dict = {"path": str(path), "line_count": len(tail_lines)}
    if structured and mode in ("journal", "tester"):
        out["records"] = list(iter_journal_lines("\n".join(tail_lines)))
    else:
        out["content"] = "\n".join(tail_lines)
    return out


@mcp.tool()
def deploy_ea(source_ex: str, name: Optional[str] = None) -> dict:
    """Copy compiled .ex4/.ex5 binary into Experts/.

    Args:
        source_ex: Path to compiled .ex4/.ex5.
        name: Optional rename target.
    """
    L = layout()
    src = Path(source_ex)
    if not src.exists():
        return {"error": f"binary not found: {src}"}
    if not L.experts_dir.exists():
        return {"error": f"Experts dir missing: {L.experts_dir}"}
    target = L.experts_dir / (name or src.name)
    shutil.copy2(src, target)
    return {"copied_to": str(target), "size": target.stat().st_size}


@mcp.tool()
def install_include(source: str, target_name: Optional[str] = None) -> dict:
    """Copy a .mqh into the terminal Include folder (e.g. for LiveLog.mqh).

    Args:
        source: Absolute path to source .mqh.
        target_name: Optional rename.
    """
    L = layout()
    src = Path(source)
    if not src.exists():
        return {"error": f"source not found: {src}"}
    L.include_dir.mkdir(parents=True, exist_ok=True)
    target = L.include_dir / (target_name or src.name)
    shutil.copy2(src, target)
    return {"copied_to": str(target)}


@mcp.tool()
def list_experts(pattern: str = "*.ex5", recurse: bool = True) -> dict:
    """List compiled EAs in Experts/."""
    L = layout()
    if not L.experts_dir.exists():
        return {"error": f"Experts dir missing: {L.experts_dir}"}
    glob = L.experts_dir.rglob if recurse else L.experts_dir.glob
    files = [{"name": p.name, "rel": str(p.relative_to(L.experts_dir)),
              "size": p.stat().st_size,
              "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat()}
             for p in glob(pattern)]
    return {"count": len(files), "files": files[:200]}


@mcp.tool()
def read_tester_report(path: Optional[str] = None, raw_truncate: int = 50000) -> dict:
    """Locate and parse latest MT5 tester HTML report.

    Args:
        path: Explicit report path. If omitted, find latest *.htm under Tester/.
        raw_truncate: Max chars of raw HTML returned.
    """
    L = layout()
    if path:
        p = Path(path)
    else:
        if not L.tester_dir.exists():
            return {"error": f"tester dir missing: {L.tester_dir}"}
        reports = sorted(L.tester_dir.rglob("*.htm*"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not reports:
            return {"error": "no tester reports"}
        p = reports[0]

    if not p.exists():
        return {"error": f"report not found: {p}"}
    html = read_text_auto(p)
    parsed = parse_tester_report(html)
    return {
        "path": str(p),
        "size": len(html),
        "summary": parsed["summary"],
        "trade_rows_detected": parsed["trade_rows_detected"],
        "trades_sample": parsed["trades_sample"],
        "raw_truncated": html[:raw_truncate],
    }


@mcp.tool()
def patch_tester_ini(config: str, updates: dict) -> dict:
    """Update fields in a tester.ini file in-place.

    Args:
        config: Path to tester.ini.
        updates: Mapping of `Section.Key` → value (e.g. {"Tester.Symbol": "EURUSD", "Tester.FromDate": "2025.01.01"}).

    Returns dict listing applied + skipped keys.
    """
    p = Path(config)
    if not p.exists():
        return {"error": f"config not found: {p}"}

    lines = p.read_text(encoding="utf-8").splitlines()
    applied: list[str] = []
    skipped: list[str] = []
    section_keys: dict[str, dict[str, str]] = {}
    for k, v in updates.items():
        if "." not in k:
            skipped.append(k)
            continue
        sec, key = k.split(".", 1)
        section_keys.setdefault(sec, {})[key] = str(v)

    out = []
    current_section = ""
    pending_remaining = {s: dict(d) for s, d in section_keys.items()}
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            # Flush remaining keys for previous section
            if current_section in pending_remaining:
                for k, v in pending_remaining[current_section].items():
                    out.append(f"{k}={v}")
                    applied.append(f"{current_section}.{k}")
                pending_remaining[current_section] = {}
            current_section = s[1:-1]
            out.append(line)
            continue

        m_eq = s.split("=", 1) if "=" in s and not s.startswith(";") else None
        if m_eq and current_section in pending_remaining:
            key = m_eq[0].strip()
            if key in pending_remaining[current_section]:
                v = pending_remaining[current_section].pop(key)
                out.append(f"{key}={v}")
                applied.append(f"{current_section}.{key}")
                continue
        out.append(line)

    # Flush whatever remains for last section
    if current_section in pending_remaining:
        for k, v in pending_remaining[current_section].items():
            out.append(f"{k}={v}")
            applied.append(f"{current_section}.{k}")
        pending_remaining[current_section] = {}

    # Sections never seen → append at end
    for sec, kv in pending_remaining.items():
        if not kv:
            continue
        out.append("")
        out.append(f"[{sec}]")
        for k, v in kv.items():
            out.append(f"{k}={v}")
            applied.append(f"{sec}.{k}")

    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"applied": applied, "skipped": skipped, "config": str(p)}


@mcp.tool()
def compile_and_deploy(source: str, ea_name: Optional[str] = None) -> dict:
    """Compile then deploy resulting .ex5/.ex4 to Experts/ in one shot."""
    res = compile(source)
    if not res.get("ok"):
        return {"compile": res, "deploy": None, "ok": False}

    src = Path(source)
    ext = ".ex5" if layout().edition == "mt5" else ".ex4"
    binary = src.with_suffix(ext)
    if not binary.exists():
        return {"compile": res, "deploy": {"error": f"binary not found: {binary}"}, "ok": False}

    deploy_res = deploy_ea(str(binary), name=ea_name)
    return {"compile": res, "deploy": deploy_res, "ok": "error" not in deploy_res}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
