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

from .paths import detect_layout, MT5Layout, list_terminal_origins, find_terminal_for_install
from .parsers import (
    parse_compile_log,
    parse_tester_report,
    read_text_auto,
    iter_journal_lines,
)
from . import analysis as _analysis
from . import lint as _lint
from . import formatting as _formatting
from . import refactor as _refactor
from . import optimization as _optimization
from . import reports as _reports
from . import snapshot as _snapshot
from . import smoke as _smoke
from . import ast_refactor as _ast_refactor

mcp = FastMCP("mt5")

# Layout resolved lazily so tests can override env first
_layout_cache: Optional[MT5Layout] = None


def layout() -> MT5Layout:
    global _layout_cache
    if _layout_cache is None:
        _layout_cache = detect_layout()
    return _layout_cache


def _workdir(source: Path) -> Path:
    """Return a hidden working directory for compile logs / smoke ini files.

    Resolution order:
      1. `MT5_WORK_DIR` env var (absolute path)
      2. `<source-parent>/.mt5tmp/`

    Directory is created if missing. Add `.mt5tmp/` to `.gitignore` to keep it out of VCS.
    """
    explicit = os.environ.get("MT5_WORK_DIR")
    if explicit:
        d = Path(explicit)
    else:
        d = source.parent / ".mt5tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    log_path = Path(log_file) if log_file else (_workdir(src) / f"{src.stem}.compile.log")

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


# ---------------------------------------------------------------------------
# Source analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def extract_inputs(source: str) -> dict:
    """Parse `input <type> <name> = <default>;` declarations from a source file."""
    return {"file": source, "inputs": _analysis.extract_inputs(source)}


@mcp.tool()
def gen_tester_inputs(source: str, write_to: Optional[str] = None) -> dict:
    """Generate a `[TesterInputs]` block from EA inputs.

    If `write_to` points at a tester.ini, the block is appended/replaced in-place.
    """
    block = _analysis.gen_tester_inputs(source)
    out: dict = {"block": block, "input_count": block.count("\n") - 1 if block else 0}
    if write_to:
        target = Path(write_to)
        if target.exists():
            text = target.read_text(encoding="utf-8")
            if "[TesterInputs]" in text:
                head = text.split("[TesterInputs]", 1)[0].rstrip()
                target.write_text(head + "\n\n" + block, encoding="utf-8")
            else:
                target.write_text(text.rstrip() + "\n\n" + block, encoding="utf-8")
            out["written_to"] = str(target)
    return out


@mcp.tool()
def resolve_includes(source: str, mql_root: Optional[str] = None) -> dict:
    """Recursively resolve `#include` directives. Reports unresolved files."""
    L = layout()
    return _analysis.resolve_includes(source, mql_root or str(L.mql_root))


@mcp.tool()
def find_symbol(symbol: str, root: str, exts: Optional[list[str]] = None,
                limit: int = 200) -> dict:
    """Grep a symbol across MQL files, skipping comments and string literals."""
    matches = _analysis.find_symbol(
        symbol, root,
        exts=tuple(exts) if exts else (".mq4", ".mq5", ".mqh"),
        limit=limit,
    )
    return {"symbol": symbol, "root": root, "match_count": len(matches), "matches": matches}


@mcp.tool()
def code_metrics(source: Optional[str] = None, root: Optional[str] = None) -> dict:
    """Compute LOC/function/nesting metrics for a file or every MQL file under a root."""
    if source:
        return _analysis.code_metrics(source)
    if root:
        return _analysis.aggregate_metrics(root)
    return {"error": "provide either source or root"}


@mcp.tool()
def extract_doc(source: str) -> dict:
    """Extract MetaEditor `//+--+ //| ... +--+` doc blocks from a source file."""
    return {"file": source, "blocks": _analysis.extract_doc(source)}


@mcp.tool()
def find_magic_collision(root: str, var_pattern: str = "Magic") -> dict:
    """Find duplicate magic-number assignments across the project."""
    return _analysis.find_magic_collision(root, var_pattern=var_pattern)


# ---------------------------------------------------------------------------
# Lint / validation
# ---------------------------------------------------------------------------

@mcp.tool()
def syntax_check(source: str, timeout_sec: int = 60) -> dict:
    """Compile a source via MetaEditor's syntax-only mode (`/s`) and return diagnostics."""
    L = layout()
    src = Path(source)
    if not src.exists():
        return {"error": f"source not found: {src}"}
    if not L.metaeditor.exists():
        return {"error": f"MetaEditor missing: {L.metaeditor}"}

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


@mcp.tool()
def lint_basic(source: str) -> dict:
    """Run structural lint rules (missing handlers, unused inputs, hardcoded magic/symbol)."""
    return _lint.lint_basic(source)


@mcp.tool()
def check_deprecated(source: str) -> dict:
    """Flag MT4-style deprecated API calls in MT5 source."""
    return {"file": source, "findings": _lint.check_deprecated(source)}


@mcp.tool()
def validate_tester_ini(config: str, source: Optional[str] = None) -> dict:
    """Sanity-check a tester.ini. If `source` given, cross-check inputs vs EA declarations."""
    return _lint.validate_tester_ini(config, source=source)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

@mcp.tool()
def format_mql(source: str, style: Optional[str] = None, write: bool = True) -> dict:
    """Format an MQL file via clang-format (treats source as C++)."""
    return _formatting.format_mql(source, style=style, write=write)


@mcp.tool()
def format_check(source: str, style: Optional[str] = None) -> dict:
    """Report whether a file needs formatting without writing it."""
    return _formatting.format_check(source, style=style)


# ---------------------------------------------------------------------------
# Refactor
# ---------------------------------------------------------------------------

@mcp.tool()
def rename_symbol(old: str, new: str, root: str, dry_run: bool = True) -> dict:
    """Rename a symbol across MQL files (whole-word match). `dry_run=True` previews only."""
    return _refactor.rename_symbol(old, new, root, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------

@mcp.tool()
def parse_optimization(path: Optional[str] = None) -> dict:
    """Parse the latest `.opt` file in the Tester folder, or one given by `path`."""
    L = layout()
    target = path or _optimization.find_latest_opt(L.tester_dir)
    if not target:
        return {"error": "no .opt file found"}
    return _optimization.parse_opt_file(target)


@mcp.tool()
def top_passes(opt_path: Optional[str] = None, criterion: str = "profit",
               n: int = 10, descending: bool = True) -> dict:
    """Sort optimization passes by criterion and return the top N."""
    parsed = parse_optimization(opt_path)
    passes = parsed.get("passes_sample") or []
    return {
        "criterion": criterion,
        "n": n,
        "top": _optimization.top_passes(passes, criterion=criterion, n=n, descending=descending),
    }


# ---------------------------------------------------------------------------
# Reports comparison
# ---------------------------------------------------------------------------

@mcp.tool()
def compare_reports(baseline: str, candidate: str) -> dict:
    """Diff two MT5 tester HTML reports key-by-key with absolute and percent deltas."""
    return _reports.compare_reports(baseline, candidate)


@mcp.tool()
def regression_check(baseline: str, candidate: str, guards: Optional[dict] = None) -> dict:
    """Verify candidate report stays within guard thresholds vs baseline."""
    return _reports.regression_check(baseline, candidate, guards=guards)


# ---------------------------------------------------------------------------
# Source snapshots
# ---------------------------------------------------------------------------

@mcp.tool()
def snapshot_sources(sources: list[str], dest: str, label: Optional[str] = None) -> dict:
    """Freeze a copy of source files into a timestamped folder under `dest`."""
    return _snapshot.snapshot_sources(sources, dest, label=label)


@mcp.tool()
def list_snapshots(dest: str) -> dict:
    """List all snapshot folders under `dest`."""
    return {"snapshots": _snapshot.list_snapshots(dest)}


# ---------------------------------------------------------------------------
# Terminal selection
# ---------------------------------------------------------------------------

@mcp.tool()
def select_terminal(origin: Optional[str] = None, hash: Optional[str] = None,
                    install: Optional[str] = None, edition: str = "mt5") -> dict:
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
            return {"error": f"no terminal data folder found for origin: {origin}"}

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

@mcp.tool()
def smoke_test(source: str, expert_name: Optional[str] = None,
               symbol: str = "EURUSD", period: str = "M15", days: int = 1,
               timeout_sec: int = 600) -> dict:
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

@mcp.tool()
def extract_function(source: str, line_start: int, line_end: int, new_name: str,
                     return_type: str = "void", target_file: Optional[str] = None,
                     dry_run: bool = True) -> dict:
    """Extract a contiguous block of lines into a new helper function.

    Brace-counting + regex param detection — not a full AST parser. Returns the
    proposed helper, call site, and parameter list. Set `dry_run=False` to write.
    """
    return _ast_refactor.extract_function(
        source, line_start, line_end, new_name,
        return_type=return_type, target_file=target_file, dry_run=dry_run,
    )


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
