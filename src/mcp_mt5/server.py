"""MCP server wrapping MetaTrader 4/5 build pipeline (compile, deploy, backtest, logs)."""
from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .paths import detect_layout, MT5Layout, list_terminal_origins, find_terminal_for_install
from .parsers import (
    parse_compile_log,
    parse_tester_journal_notes,
    parse_tester_report,
    read_text_auto,
    write_text_preserving,
    iter_journal_lines,
)
from .workdir import workdir
from . import analysis as _analysis
from . import lint as _lint
from . import formatting as _formatting
from . import refactor as _refactor
from . import optimization as _optimization
from . import reports as _reports
from . import snapshot as _snapshot
from . import smoke as _smoke
from . import ast_refactor as _ast_refactor

_INSTRUCTIONS = """\
MetaTrader 4/5 build-and-test harness. It drives MetaEditor64.exe and terminal64.exe on this
Windows machine; it never connects to a broker or places live orders.

Typical loop: env_info -> compile (or compile_and_deploy) -> patch_tester_ini -> run_backtest
-> read_tester_report -> compare_reports / regression_check -> edit source -> repeat.
smoke_test does compile + deploy + 1-day backtest + journal scan in one call.

Rules:
- Every path argument must be an absolute Windows path (e.g. C:\\Users\\me\\EA\\MyEA.mq5).
- Tools that modify files default to a dry run (rename_symbol, extract_function, format_mql);
  pass dry_run=false / write=true to apply.
- run_backtest blocks until the terminal exits; needs ShutdownTerminal=1 in the ini and can take
  5-30 minutes. A backtest is only comparable to another if journal_notes shows no
  start_time_changed warning.
- A failed compile, missing file or bad argument is reported as a tool error; a backtest that
  completes with poor results is a normal result.
"""

mcp = FastMCP("mt5", instructions=_INSTRUCTIONS)

# Tool annotation presets (hints for the client; never a security boundary).
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_RUN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def _raise_if_error(result: dict) -> dict[str, Any]:
    """Turn a module-level `{"error": ...}` result into a tool error the client can see."""
    if isinstance(result, dict) and result.get("error") and result.get("ok") is not True:
        raise ToolError(str(result["error"]))
    return result

# Layout resolved lazily so tests can override env first
_layout_cache: Optional[MT5Layout] = None


def layout() -> MT5Layout:
    global _layout_cache
    if _layout_cache is None:
        _layout_cache = detect_layout()
    return _layout_cache


_workdir = workdir  # shared with smoke.py
_spawned_pids = _smoke.spawned_pids  # terminals launched by this process


def reset_layout_cache() -> None:
    """Forget the cached layout (tests and `select_terminal` use this)."""
    global _layout_cache
    _layout_cache = None


def _ini_get(path: Path, section: str, key: str) -> Optional[str]:
    """Return `key` from `[section]` of an ini file (any encoding), or None."""
    current = ""
    for raw in read_text_auto(path).splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current.lower() == section.lower() and "=" in line:
            k, v = line.split("=", 1)
            if k.strip().lower() == key.lower():
                return v.split(";", 1)[0].strip()
    return None


def _report_path_from_ini(cfg: Path, L: MT5Layout, portable: bool = False) -> Optional[Path]:
    """Where the terminal writes the report named by `Report=` in `cfg`.

    MT5 resolves `Report=` relative to the *platform directory*: the terminal data
    folder in normal mode, the install folder in /portable mode. It is not `Tester/`.
    A missing extension means `.htm` for a single test and `.xml` for an optimisation.
    """
    name = _ini_get(cfg, "Tester", "Report")
    if not name:
        return None
    optimization = (_ini_get(cfg, "Tester", "Optimization") or "0").strip() not in ("", "0")
    rel = Path(name.strip('"').replace("\\", "/"))  # ini paths use backslashes; Path on POSIX would not split them
    if not rel.suffix:
        rel = rel.with_suffix(".xml" if optimization else ".htm")
    base = L.install if portable else L.data
    return rel if rel.is_absolute() else base / rel


def _find_reports(L: MT5Layout, since: Optional[float] = None) -> list[Path]:
    """Tester HTML reports, newest first, from every location the terminal writes to."""
    candidates: list[Path] = []
    for folder in (L.data, L.install):
        if folder.exists():
            candidates.extend(p for p in folder.glob("*.htm*") if p.is_file())
    if L.tester_dir.exists():
        candidates.extend(p for p in L.tester_dir.rglob("*.htm*") if p.is_file())
    seen: set[str] = set()
    unique = []
    for p in candidates:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    if since is not None:
        unique = [p for p in unique if p.stat().st_mtime >= since - 1]
    unique.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return unique


@mcp.tool(annotations=_RO)
def env_info() -> dict[str, Any]:
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


@mcp.tool(annotations=_RO)
def list_terminals() -> dict[str, Any]:
    """Enumerate all MetaTrader terminal data folders under %APPDATA%\\MetaQuotes\\Terminal."""
    terminals = list_terminal_origins()
    if not terminals:
        return {"count": 0, "terminals": [], "note": "APPDATA not set or no MetaQuotes\\Terminal folder found"}
    return {"count": len(terminals), "terminals": terminals}


@mcp.tool(annotations=_RUN)
def compile(
    source: str,
    include: Optional[str] = None,
    log_file: Optional[str] = None,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    """Compile a .mq4/.mq5/.mqh source via MetaEditor CLI.

    Args:
        source: Absolute path to the source file.
        include: Optional MQL root override (parent of `Include/`). Defaults to terminal MQL root.
        log_file: Optional explicit log path. Defaults to `.mt5tmp/<stem>.compile.log` next to the source.
        timeout_sec: Subprocess timeout.

    Returns: returncode, structured `errors`/`warnings` lists, `result_errors`/`result_warnings`,
             `log_path`, `log_excerpt` (last 80 lines), `cmd`.
    """
    L = layout()
    src = Path(source)
    if not src.exists():
        raise ToolError(f"source not found: {src}")
    if not L.metaeditor.exists():
        raise ToolError(f"MetaEditor missing: {L.metaeditor}")

    inc = Path(include) if include else L.mql_root
    log_path = Path(log_file) if log_file else (_workdir(src) / f"{src.stem}.compile.log")

    cmd = [
        str(L.metaeditor),
        f"/compile:{src}",
        f"/include:{inc}",
        f"/log:{log_path}",
    ]
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        rc = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        rc = -1
        stdout = ""
        stderr = f"timeout after {timeout_sec}s: {e}"

    parsed = {"errors": [], "warnings": [], "result_errors": None, "result_warnings": None,
              "result_line_found": False, "ok": False}
    excerpt = ""
    if log_path.exists():
        try:
            text = read_text_auto(log_path)
            parsed = parse_compile_log(text)
            excerpt = "\n".join(text.splitlines()[-80:])
        except Exception as e:
            excerpt = f"(log read failed: {e})"

    # A binary is only produced for main programs; require it to be newer than this invocation
    # so a stale .ex5 from an earlier build can never make a silent failure look like success.
    binary: Optional[Path] = None
    binary_fresh: Optional[bool] = None
    if src.suffix.lower() in (".mq4", ".mq5"):
        binary = src.with_suffix(".ex5" if L.edition == "mt5" else ".ex4")
        binary_fresh = binary.exists() and binary.stat().st_mtime >= started - 1
    ok = bool(parsed["ok"] and (binary_fresh if binary_fresh is not None else True))

    return {
        "returncode": rc,
        "cmd": " ".join(cmd),
        "log_path": str(log_path),
        "ok": ok,
        "result_line_found": parsed["result_line_found"],
        "binary_path": str(binary) if binary else None,
        "binary_fresh": binary_fresh,
        "result_errors": parsed["result_errors"],
        "result_warnings": parsed["result_warnings"],
        "errors": parsed["errors"][:50],
        "warnings": parsed["warnings"][:50],
        "log_excerpt": excerpt,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


@mcp.tool(annotations=_RUN)
def run_backtest(
    config: str,
    wait: bool = True,
    timeout_sec: int = 1800,
    portable: bool = False,
) -> dict[str, Any]:
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
        raise ToolError(f"config not found: {cfg}")
    if not L.terminal.exists():
        raise ToolError(f"terminal missing: {L.terminal}")

    cmd = [str(L.terminal), f"/config:{cfg}"]
    if portable:
        cmd.append("/portable")

    start = time.time()
    rc: Optional[int] = None
    pid: Optional[int] = None
    extra = ("/portable",) if portable else ()
    if wait:
        rc, pid = _smoke.run_terminal(L.terminal, cfg, timeout_sec, extra_args=extra)
        if rc is None:
            rc = -1
    else:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pid = proc.pid
        _spawned_pids.add(pid)

    elapsed = round(time.time() - start, 2)
    latest_log = None
    journal: dict = {"notes": {}, "warnings": []}
    if L.tester_logs.exists():
        logs = [p for p in L.tester_logs.glob("*.log") if p.stat().st_mtime >= start - 1]
        logs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            latest_log = str(logs[0])
            try:
                journal = parse_tester_journal_notes(read_text_auto(logs[0]))
            except Exception as e:  # pragma: no cover
                journal = {"notes": {}, "warnings": [f"journal parse failed: {e}"]}

    expected_report = _report_path_from_ini(cfg, L, portable=portable)
    report_path = None
    if expected_report is not None and expected_report.exists() and expected_report.stat().st_mtime >= start - 1:
        report_path = str(expected_report)
    elif wait:
        fresh = _find_reports(L, since=start)
        report_path = str(fresh[0]) if fresh else None

    return {
        "returncode": rc,
        "pid": pid,
        "elapsed_sec": elapsed,
        "cmd": " ".join(cmd),
        "latest_tester_log": latest_log,
        "expected_report": str(expected_report) if expected_report else None,
        "report_path": report_path,
        "journal_notes": journal["notes"],
        "warnings": journal["warnings"],
    }


@mcp.tool(annotations=_DESTRUCTIVE)
def kill_terminal(all_instances: bool = False) -> dict[str, Any]:
    """Force-kill terminal processes launched by this server (run_backtest / smoke_test).

    By default only PIDs this server started are killed, so a live-trading terminal on the
    same machine is never touched. Pass `all_instances=True` to `taskkill` every
    terminal64.exe/terminal.exe of the configured edition (destructive).
    """
    L = layout()
    results = []
    try:
        if _spawned_pids and not all_instances:
            for pid in sorted(_spawned_pids):
                proc = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True)
                results.append({"pid": pid, "returncode": proc.returncode, "stdout": proc.stdout.strip()})
            _spawned_pids.clear()
            return {"killed": results, "scope": "spawned_by_server"}
        if not all_instances:
            return {"killed": [], "scope": "spawned_by_server",
                    "note": "no terminal launched by this server is running; pass all_instances=True to kill every instance"}
        proc = subprocess.run(["taskkill", "/F", "/IM", L.terminal.name], capture_output=True, text=True)
        _spawned_pids.clear()
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "scope": "all_instances"}
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool(annotations=_RO)
def tail_log(mode: str = "live", lines: int = 100, date: Optional[str] = None,
             structured: bool = False) -> dict[str, Any]:
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
            raise ToolError(f"tester logs dir missing: {L.tester_logs}")
        files = sorted(L.tester_logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise ToolError("no tester logs")
        path = files[0]
    else:
        raise ToolError(f"unknown mode: {mode}")

    if not path.exists():
        raise ToolError(f"log not found: {path}")

    text = read_text_auto(path)
    tail_lines = text.splitlines()[-lines:]
    out: dict = {"path": str(path), "line_count": len(tail_lines)}
    if structured and mode in ("journal", "tester"):
        out["records"] = list(iter_journal_lines("\n".join(tail_lines)))
    else:
        out["content"] = "\n".join(tail_lines)
    return out


@mcp.tool(annotations=_DESTRUCTIVE)
def deploy_ea(source_ex: str, name: Optional[str] = None) -> dict[str, Any]:
    """Copy compiled .ex4/.ex5 binary into Experts/.

    Args:
        source_ex: Path to compiled .ex4/.ex5.
        name: Optional rename target.
    """
    L = layout()
    src = Path(source_ex)
    if not src.exists():
        raise ToolError(f"binary not found: {src}")
    if not L.experts_dir.exists():
        raise ToolError(f"Experts dir missing: {L.experts_dir}")
    target = L.experts_dir / (name or src.name)
    shutil.copy2(src, target)
    return {"copied_to": str(target), "size": target.stat().st_size}


@mcp.tool(annotations=_DESTRUCTIVE)
def install_include(source: str, target_name: Optional[str] = None) -> dict[str, Any]:
    """Copy a .mqh into the terminal Include folder (e.g. for LiveLog.mqh).

    Args:
        source: Absolute path to source .mqh.
        target_name: Optional rename.
    """
    L = layout()
    src = Path(source)
    if not src.exists():
        raise ToolError(f"source not found: {src}")
    L.include_dir.mkdir(parents=True, exist_ok=True)
    target = L.include_dir / (target_name or src.name)
    shutil.copy2(src, target)
    return {"copied_to": str(target)}


@mcp.tool(annotations=_RO)
def list_experts(pattern: Optional[str] = None, recurse: bool = True) -> dict[str, Any]:
    """List compiled EAs in Experts/ (defaults to *.ex5 for MT5, *.ex4 for MT4)."""
    L = layout()
    if not pattern:
        pattern = "*.ex5" if L.edition == "mt5" else "*.ex4"
    if not L.experts_dir.exists():
        raise ToolError(f"Experts dir missing: {L.experts_dir}")
    glob = L.experts_dir.rglob if recurse else L.experts_dir.glob
    files = [{"name": p.name, "rel": str(p.relative_to(L.experts_dir)),
              "size": p.stat().st_size,
              "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat()}
             for p in glob(pattern)]
    return {"count": len(files), "files": files[:200]}


@mcp.tool(annotations=_RO)
def read_tester_report(path: Optional[str] = None, raw_truncate: int = 50000,
                       max_trades: int = 500) -> dict[str, Any]:
    """Locate and parse the latest MT5 tester HTML report.

    Args:
        path: Explicit report path. If omitted, the newest *.htm in the terminal data folder
              root (where `Report=` is written), the install dir (/portable) and Tester/ is used.
        raw_truncate: Max chars of raw HTML returned.
        max_trades: Max trade rows returned in `trades` (all rows are parsed).
    """
    L = layout()
    if path:
        p = Path(path)
    else:
        reports = _find_reports(L)
        if not reports:
            raise ToolError("no tester reports found in data folder, install dir or Tester/")
        p = reports[0]

    if not p.exists():
        raise ToolError(f"report not found: {p}")
    html = read_text_auto(p)
    parsed = parse_tester_report(html, max_trades=max_trades)
    return {
        "path": str(p),
        "size": len(html),
        "summary": parsed["summary"],
        "trade_rows_detected": parsed["trade_rows_detected"],
        "trades_sample": parsed["trades_sample"],
        "trades": parsed["trades"],
        "raw_truncated": html[:raw_truncate],
    }


@mcp.tool(annotations=_DESTRUCTIVE)
def patch_tester_ini(config: str, updates: dict) -> dict[str, Any]:
    """Update fields in a tester.ini file in-place.

    Args:
        config: Path to tester.ini.
        updates: Mapping of `Section.Key` → value (e.g. {"Tester.Symbol": "EURUSD", "Tester.FromDate": "2025.01.01"}).

    Returns dict listing applied + skipped keys.
    """
    p = Path(config)
    if not p.exists():
        raise ToolError(f"config not found: {p}")

    lines = read_text_auto(p).splitlines()
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

    encoding = write_text_preserving(p, "\n".join(out) + "\n")
    return {"applied": applied, "skipped": skipped, "config": str(p), "encoding": encoding}


@mcp.tool(annotations=_DESTRUCTIVE)
def compile_and_deploy(source: str, ea_name: Optional[str] = None) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Source analysis
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_RO)
def extract_inputs(source: str) -> dict[str, Any]:
    """Parse `input <type> <name> = <default>;` declarations from a source file."""
    return {"file": source, "inputs": _analysis.extract_inputs(source)}


@mcp.tool(annotations=_DESTRUCTIVE)
def gen_tester_inputs(source: str, write_to: Optional[str] = None) -> dict[str, Any]:
    """Generate a `[TesterInputs]` block from EA inputs.

    If `write_to` points at a tester.ini, the block is appended/replaced in-place.
    """
    block = _analysis.gen_tester_inputs(source)
    out: dict = {"block": block, "input_count": block.count("\n") - 1 if block else 0}
    if write_to:
        target = Path(write_to)
        if target.exists():
            text = read_text_auto(target)
            if "[TesterInputs]" in text:
                head = text.split("[TesterInputs]", 1)[0].rstrip()
                write_text_preserving(target, head + "\n\n" + block)
            else:
                write_text_preserving(target, text.rstrip() + "\n\n" + block)
            out["written_to"] = str(target)
    return out


@mcp.tool(annotations=_RO)
def resolve_includes(source: str, mql_root: Optional[str] = None) -> dict[str, Any]:
    """Recursively resolve `#include` directives. Reports unresolved files."""
    L = layout()
    return _analysis.resolve_includes(source, mql_root or str(L.mql_root))


@mcp.tool(annotations=_RO)
def find_symbol(symbol: str, root: str, exts: Optional[list[str]] = None,
                limit: int = 200) -> dict[str, Any]:
    """Grep a symbol across MQL files, skipping comments and string literals."""
    matches = _analysis.find_symbol(
        symbol, root,
        exts=tuple(exts) if exts else (".mq4", ".mq5", ".mqh"),
        limit=limit,
    )
    return {"symbol": symbol, "root": root, "match_count": len(matches), "matches": matches}


@mcp.tool(annotations=_RO)
def code_metrics(source: Optional[str] = None, root: Optional[str] = None) -> dict[str, Any]:
    """Compute LOC/function/nesting metrics for a file or every MQL file under a root."""
    if source:
        return _raise_if_error(_analysis.code_metrics(source))
    if root:
        return _raise_if_error(_analysis.aggregate_metrics(root))
    raise ToolError("provide either source or root")


@mcp.tool(annotations=_RO)
def extract_doc(source: str) -> dict[str, Any]:
    """Extract MetaEditor `//+--+ //| ... +--+` doc blocks from a source file."""
    return {"file": source, "blocks": _analysis.extract_doc(source)}


@mcp.tool(annotations=_RO)
def find_magic_collision(root: str, var_pattern: str = "Magic") -> dict[str, Any]:
    """Find duplicate magic-number assignments across the project."""
    return _analysis.find_magic_collision(root, var_pattern=var_pattern)


# ---------------------------------------------------------------------------
# Lint / validation
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_RUN)
def syntax_check(source: str, timeout_sec: int = 60) -> dict[str, Any]:
    """Compile a source via MetaEditor's syntax-only mode (`/s`) and return diagnostics."""
    L = layout()
    src = Path(source)
    if not src.exists():
        raise ToolError(f"source not found: {src}")
    if not L.metaeditor.exists():
        raise ToolError(f"MetaEditor missing: {L.metaeditor}")

    log_path = _workdir(src) / f"{src.stem}.syntax.log"
    cmd = [str(L.metaeditor), "/s", f"/compile:{src}", f"/include:{L.mql_root}", f"/log:{log_path}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    parsed = {"errors": [], "warnings": [], "result_errors": None, "result_warnings": None, "ok": False}
    excerpt = ""
    if log_path.exists():
        text = read_text_auto(log_path)
        parsed = parse_compile_log(text)
        excerpt = "\n".join(text.splitlines()[-40:])
    return {
        "returncode": rc,
        "ok": parsed["ok"],
        "errors": parsed["errors"][:50],
        "warnings": parsed["warnings"][:50],
        "result_errors": parsed["result_errors"],
        "result_warnings": parsed["result_warnings"],
        "log_excerpt": excerpt,
    }


@mcp.tool(annotations=_RO)
def lint_basic(source: str) -> dict[str, Any]:
    """Run structural lint rules (missing handlers, unused inputs, hardcoded magic/symbol)."""
    return _raise_if_error(_lint.lint_basic(source))


@mcp.tool(annotations=_RO)
def check_deprecated(source: str) -> dict[str, Any]:
    """Flag MT4-style deprecated API calls in MT5 source."""
    return {"file": source, "findings": _lint.check_deprecated(source)}


@mcp.tool(annotations=_RO)
def validate_tester_ini(config: str, source: Optional[str] = None) -> dict[str, Any]:
    """Sanity-check a tester.ini. If `source` given, cross-check inputs vs EA declarations."""
    return _raise_if_error(_lint.validate_tester_ini(config, source=source))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_DESTRUCTIVE)
def format_mql(source: str, style: Optional[str] = None, write: bool = True) -> dict[str, Any]:
    """Format an MQL file via clang-format (treats source as C++)."""
    return _raise_if_error(_formatting.format_mql(source, style=style, write=write))


@mcp.tool(annotations=_RO)
def format_check(source: str, style: Optional[str] = None) -> dict[str, Any]:
    """Report whether a file needs formatting without writing it."""
    return _raise_if_error(_formatting.format_check(source, style=style))


# ---------------------------------------------------------------------------
# Refactor
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_DESTRUCTIVE)
def rename_symbol(old: str, new: str, root: str, dry_run: bool = True) -> dict[str, Any]:
    """Rename a symbol across MQL files (whole-word match). `dry_run=True` previews only."""
    return _raise_if_error(_refactor.rename_symbol(old, new, root, dry_run=dry_run))


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------

def _load_opt(path: Optional[str], expert: Optional[str], symbol: Optional[str],
              period: Optional[str]) -> dict[str, Any]:
    L = layout()
    target = path or _optimization.find_latest_opt(L.tester_dir, expert=expert, symbol=symbol, period=period)
    if not target:
        raise ToolError("no matching .opt file found under Tester/ (caches auto-delete after 30 days unused)")
    parsed = _optimization.parse_opt_file(target)
    if parsed.get("error") and not parsed.get("passes"):
        raise ToolError(f"{parsed['error']} ({parsed.get('path', target)})")
    return parsed


@mcp.tool(annotations=_RO)
def parse_optimization(path: Optional[str] = None, expert: Optional[str] = None,
                       symbol: Optional[str] = None, period: Optional[str] = None,
                       sample: int = 50) -> dict[str, Any]:
    """Parse an MT5 `.opt` optimisation cache (header, inputs, pass count, first `sample` passes).

    Without `path`, the newest cache under Tester/ is used; pass `expert`/`symbol`/`period`
    to select deterministically by the documented filename schema. Use `top_passes` for ranking
    over the complete pass list.
    """
    parsed = _load_opt(path, expert, symbol, period)
    passes = parsed.pop("passes", None)
    if passes is not None:
        parsed["passes_sample"] = passes[:sample]
    return parsed


@mcp.tool(annotations=_RO)
def top_passes(opt_path: Optional[str] = None, criterion: str = "profit",
               n: int = 10, descending: bool = True, expert: Optional[str] = None,
               symbol: Optional[str] = None, period: Optional[str] = None) -> dict[str, Any]:
    """Sort *all* optimization passes by criterion and return the top N."""
    parsed = _load_opt(opt_path, expert, symbol, period)
    passes = parsed.get("passes") or []
    if not passes:
        raise ToolError(f"{parsed.get('error', 'no passes parsed')} ({parsed.get('path')})")
    return {
        "path": parsed.get("path"),
        "criterion": criterion,
        "n": n,
        "pass_count": len(passes),
        "top": _optimization.top_passes(passes, criterion=criterion, n=n, descending=descending),
    }


# ---------------------------------------------------------------------------
# Reports comparison
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_RO)
def compare_reports(baseline: str, candidate: str) -> dict[str, Any]:
    """Diff two MT5 tester HTML reports key-by-key with absolute and percent deltas."""
    return _raise_if_error(_reports.compare_reports(baseline, candidate))


@mcp.tool(annotations=_RO)
def regression_check(baseline: str, candidate: str, guards: Optional[dict] = None) -> dict[str, Any]:
    """Verify candidate report stays within guard thresholds vs baseline."""
    return _raise_if_error(_reports.regression_check(baseline, candidate, guards=guards))


# ---------------------------------------------------------------------------
# Source snapshots
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_WRITE)
def snapshot_sources(sources: list[str], dest: str, label: Optional[str] = None) -> dict[str, Any]:
    """Freeze a copy of source files into a timestamped folder under `dest`."""
    return _snapshot.snapshot_sources(sources, dest, label=label)


@mcp.tool(annotations=_RO)
def list_snapshots(dest: str) -> dict[str, Any]:
    """List all snapshot folders under `dest`."""
    return {"snapshots": _snapshot.list_snapshots(dest)}


# ---------------------------------------------------------------------------
# Terminal selection
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_RUN)
def select_terminal(origin: Optional[str] = None, hash: Optional[str] = None,
                    install: Optional[str] = None, edition: str = "mt5") -> dict[str, Any]:
    """Switch the active terminal data folder for this session.

    Provide one of: `origin` (install path stored in origin.txt), `hash` (32-char
    folder name), or `install` (auto-scan for the matching origin).

    Subsequent tool calls will use the new layout until the server restarts.
    """
    global _layout_cache
    target_install = Path(install) if install else None
    target_hash = hash
    if origin:
        for t in list_terminal_origins():
            if t["origin"] and t["origin"].strip().lower() == origin.strip().lower():
                target_hash = t["hash"]
                break
        if not target_hash:
            raise ToolError(f"no terminal data folder found for origin: {origin}")

    if target_install and not target_hash:
        found = find_terminal_for_install(target_install)
        if found:
            target_hash, _ = found

    layout_kwargs: dict = {"edition": edition}
    if target_install:
        layout_kwargs["install"] = str(target_install)
    if target_hash:
        layout_kwargs["terminal_hash"] = target_hash

    new_layout = detect_layout(**layout_kwargs)
    _layout_cache = new_layout
    return {
        "active_install": str(new_layout.install),
        "active_data": str(new_layout.data),
        "active_hash": new_layout.terminal_hash,
        "edition": new_layout.edition,
        "issues": new_layout.issues(),
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_DESTRUCTIVE)
def smoke_test(source: str, expert_name: Optional[str] = None,
               symbol: str = "EURUSD", period: str = "M15", days: int = 1,
               timeout_sec: int = 600) -> dict[str, Any]:
    """Compile, deploy, run a 1-day headless backtest, and scan the journal for runtime errors.

    Returns `ok: true` only if compilation, deployment, run, and the journal scan all pass.
    """
    return _smoke.run_smoke(
        layout(),
        source,
        expert_name=expert_name,
        symbol=symbol,
        period=period,
        days=days,
        timeout_sec=timeout_sec,
    )


# ---------------------------------------------------------------------------
# AST-style refactor
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_DESTRUCTIVE)
def extract_function(source: str, line_start: int, line_end: int, new_name: str,
                     return_type: str = "void", target_file: Optional[str] = None,
                     dry_run: bool = True) -> dict[str, Any]:
    """Extract a contiguous block of lines into a new helper function.

    Brace-counting + regex param detection — not a full AST parser. Returns the
    proposed helper, call site, and parameter list. Set `dry_run=False` to write.
    """
    return _raise_if_error(_ast_refactor.extract_function(
        source, line_start, line_end, new_name,
        return_type=return_type, target_file=target_file, dry_run=dry_run,
    ))


# ---------------------------------------------------------------------------
# LiveLog resource (subscription-friendly)
# ---------------------------------------------------------------------------

@mcp.resource("mt5://livelog")
def livelog_resource() -> str:
    """Latest contents of MQL5/Files/LiveLog.txt — clients can re-read for polling updates."""
    L = layout()
    path = L.files_dir / "LiveLog.txt"
    if not path.exists():
        return f"(no LiveLog at {path})"
    text = read_text_auto(path)
    return "\n".join(text.splitlines()[-500:])


@mcp.resource("mt5://journal")
def journal_resource() -> str:
    """Latest daily MT5 journal log."""
    L = layout()
    today = datetime.now().strftime("%Y%m%d")
    path = L.logs_dir / f"{today}.log"
    if not path.exists():
        return f"(no journal for {today} at {path})"
    text = read_text_auto(path)
    return "\n".join(text.splitlines()[-500:])


@mcp.resource("mt5://tester-log")
def tester_log_resource() -> str:
    """Latest Strategy Tester journal log."""
    L = layout()
    if not L.tester_logs.exists():
        return "(no tester log dir)"
    files = sorted(L.tester_logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "(no tester logs)"
    text = read_text_auto(files[0])
    return f"# {files[0].name}\n" + "\n".join(text.splitlines()[-500:])


def main():
    mcp.run()


if __name__ == "__main__":
    main()
