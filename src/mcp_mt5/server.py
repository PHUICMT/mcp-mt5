"""MCP server wrapping MetaTrader 4/5 build pipeline (compile, deploy, backtest, logs)."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal, Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .paths import detect_layout, MT5Layout, list_terminal_origins, find_terminal_for_install
from .parsers import (
    parse_compile_log,
    parse_tester_journal_notes,
    parse_tester_report,
    read_text_auto,
    write_text_preserving,
    iter_journal_lines,
)
from .winpath import win_path
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
from .runs import Run, registry as _runs

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


# Reports handed out as MCP resources instead of inline HTML: id -> path.
_report_ids: dict[str, Path] = {}


def _register_report(p: Path) -> str:
    """Return the `mt5://report/{id}` URI for a report file, registering it for `report_resource`."""
    rid = hashlib.sha1(str(p.resolve()).encode("utf-8")).hexdigest()[:12]
    _report_ids[rid] = p
    return f"mt5://report/{rid}"


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
    source: Annotated[str, Field(description='Absolute Windows path to the .mq4/.mq5/.mqh source file.')],
    include: Annotated[Optional[str], Field(description="MQL root folder (parent of Include/) if different from the active terminal's.")] = None,
    log_file: Annotated[Optional[str], Field(description='Explicit path for the compile log; defaults to .mt5tmp/<stem>.compile.log next to the source.')] = None,
    timeout_sec: Annotated[int, Field(description='Give up after this many seconds.')] = 300,
    syntax_only: Annotated[bool, Field(description='true = MetaEditor /s syntax check only: faster, produces no binary.')] = False,
) -> dict[str, Any]:
    """Compile one MQL source with MetaEditor and return structured diagnostics.

    `ok` is true only when the log has a `Result:` line with 0 errors AND (unless
    `syntax_only`) a fresh .ex5/.ex4 was produced (`binary_fresh`). Fix every entry in `errors`
    (file, line, col, code, message) and call again; warnings do not block deployment.
    Takes 1-30 s. To also copy the binary into Experts/ use `compile_and_deploy`.
    """
    L = layout()
    src = Path(source)
    if not src.exists():
        raise ToolError(f"source not found: {src}")
    if not L.metaeditor.exists():
        raise ToolError(f"MetaEditor missing: {L.metaeditor}")

    inc = Path(include) if include else L.mql_root
    suffix = "syntax" if syntax_only else "compile"
    log_path = Path(log_file) if log_file else (_workdir(src) / f"{src.stem}.{suffix}.log")

    cmd = [str(L.metaeditor), *(["/s"] if syntax_only else []), f"/compile:{win_path(src)}", f"/include:{win_path(inc)}", f"/log:{win_path(log_path)}"]
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
    if not syntax_only and src.suffix.lower() in (".mq4", ".mq5"):
        binary = src.with_suffix(".ex5" if L.edition == "mt5" else ".ex4")
        binary_fresh = binary.exists() and binary.stat().st_mtime >= started - 1
    ok = bool(parsed["ok"] and (binary_fresh if binary_fresh is not None else True))

    return {
        "returncode": rc,
        "cmd": " ".join(cmd),
        "log_path": str(log_path),
        "ok": ok,
        "syntax_only": syntax_only,
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


def _new_log_text(run: Run, folders: list[Path]) -> tuple[Optional[str], str]:
    """Text appended to the tester logs since the run started (daily files are shared by all runs)."""
    newest: Optional[Path] = None
    pieces: list[str] = []
    for folder in folders:
        if not folder.exists():
            continue
        for f in folder.rglob("*.log"):
            try:
                if f.stat().st_mtime < run.started - 1:
                    continue
                raw = f.read_bytes()
            except OSError:
                continue
            offset = run.log_offsets.get(str(f), 0)
            delta = raw[offset:] if offset < len(raw) else b""
            if not delta and offset:
                continue
            enc = "utf-16-le" if raw[:2] == b"\xff\xfe" or (b"\x00" in delta[:200]) else "utf-8"
            if enc == "utf-16-le" and offset % 2:
                delta = delta[1:]
            pieces.append(delta.decode(enc, errors="replace"))
            if newest is None or f.stat().st_mtime > newest.stat().st_mtime:
                newest = f
    return (str(newest) if newest else None), "\n".join(pieces)


def _finalize_run(L: MT5Layout, cfg: Path, portable: bool) -> "Callable[[Run], dict]":
    """Build the callback that collects report path + journal notes once a terminal exits."""
    def _collect(run: Run) -> dict:
        start = run.started
        latest_log, text = _new_log_text(run, [L.tester_logs, L.agent_logs_root, L.terminal_logs_dir])
        try:
            journal = parse_tester_journal_notes(text) if text else {"notes": {}, "warnings": []}
        except Exception as e:  # pragma: no cover
            journal = {"notes": {}, "warnings": [f"journal parse failed: {e}"]}
        expected = _report_path_from_ini(cfg, L, portable=portable)
        report_path = None
        if expected is not None and expected.exists() and expected.stat().st_mtime >= start - 1:
            report_path = str(expected)
        else:
            fresh = _find_reports(L, since=start)
            report_path = str(fresh[0]) if fresh else None
        return {
            "latest_tester_log": latest_log,
            "expected_report": str(expected) if expected else None,
            "report_path": report_path,
            "report_uri": _register_report(Path(report_path)) if report_path else None,
            "journal_notes": journal["notes"],
            "warnings": journal["warnings"],
        }
    return _collect


def _run_view(run: Run, tail_lines: int = 0) -> dict[str, Any]:
    view = {
        "run_id": run.run_id,
        "status": run.status,
        "pid": run.pid,
        "returncode": run.returncode,
        "elapsed_sec": run.elapsed_sec,
        "timeout_sec": run.timeout_sec,
        "config": run.config,
        "cmd": " ".join(run.cmd),
        "error": run.error,
    }
    view.update(run.result)
    if tail_lines and run.status == "running":
        L = layout()
        if L.tester_logs.exists():
            logs = [p for p in L.tester_logs.glob("*.log") if p.stat().st_mtime >= run.started - 1]
            if logs:
                newest = max(logs, key=lambda p: p.stat().st_mtime)
                view["latest_tester_log"] = str(newest)
                view["log_tail"] = read_text_auto(newest).splitlines()[-tail_lines:]
    return view


def _start_run(config: str, portable: bool, timeout_sec: int) -> Run:
    L = layout()
    cfg = Path(config)
    if not cfg.exists():
        raise ToolError(f"config not found: {cfg}")
    if not L.terminal.exists():
        raise ToolError(f"terminal missing: {L.terminal}")
    extra = ("/portable",) if portable else ()
    run = _runs.start(L.terminal, cfg, timeout_sec, extra, finalize=_finalize_run(L, cfg, portable),
                      watch_logs=(L.tester_logs, L.agent_logs_root, L.terminal_logs_dir))
    if run.status == "failed":
        raise ToolError(f"could not launch terminal: {run.error}")
    return run


async def run_backtest_impl(config: str, wait: bool = True, timeout_sec: int = 1800, portable: bool = False,
                            progress: "Callable[[float, float, str], Awaitable[None]] | None" = None,
                            poll_sec: float = 5.0, retry_on_history_sync: bool = True) -> dict[str, Any]:
    view = await _run_once(config, wait, timeout_sec, portable, progress, poll_sec)
    notes = view.get("journal_notes") or {}
    if (wait and retry_on_history_sync and view.get("status") == "completed"
            and ("history_sync_failed" in notes or "error" in str(notes.get("pass_error", "")))):
        # First headless start after login: the tester began before the history download finished.
        # The download has now happened, so one retry normally succeeds.
        if progress:
            await progress(0.0, float(timeout_sec), "history was still downloading; retrying the backtest once")
        retry = await _run_once(config, wait, timeout_sec, portable, progress, poll_sec)
        retry["retried_after_history_sync"] = True
        retry["first_attempt"] = {"run_id": view["run_id"], "warnings": view.get("warnings", [])}
        return retry
    return view


async def _run_once(config: str, wait: bool, timeout_sec: int, portable: bool,
                    progress: "Callable[[float, float, str], Awaitable[None]] | None", poll_sec: float) -> dict[str, Any]:
    run = _start_run(config, portable, timeout_sec)
    if progress:
        await progress(0.0, float(timeout_sec), f"terminal launched (pid {run.pid}); waiting for ShutdownTerminal")
    if not wait:
        return _run_view(run)
    while run.status == "running":
        await anyio.sleep(poll_sec)
        if progress and run.status == "running":
            await progress(min(run.elapsed_sec, timeout_sec - 0.001), float(timeout_sec),
                           f"backtest running for {int(run.elapsed_sec)} s")
    if progress:
        await progress(float(timeout_sec), float(timeout_sec), f"terminal exited ({run.status})")
    view = _run_view(run)
    if run.status == "timeout":
        view["returncode"] = -1
    return view


@mcp.tool(annotations=_RUN)
async def run_backtest(
    config: Annotated[str, Field(description='Absolute path to the tester.ini file.')],
    ctx: Context,
    wait: Annotated[bool, Field(description='true = block until the terminal exits (needs ShutdownTerminal=1); false = launch and return a run_id for get_backtest.')] = True,
    timeout_sec: Annotated[int, Field(description='Give up (and kill the terminal) after this many seconds.')] = 1800,
    portable: Annotated[bool, Field(description='Pass /portable so the terminal uses its install folder as data folder.')] = False,
) -> dict[str, Any]:
    """Run a Strategy Tester backtest headlessly from a tester.ini and wait for it to finish.

    Requires the EA to be deployed first (`compile_and_deploy` / `deploy_ea`) and
    `ShutdownTerminal=1` in the ini, otherwise the call runs until `timeout_sec`.
    Typically 1-30 minutes; progress notifications are sent every few seconds while waiting.
    If the tester reports "cannot synchronize history" (first headless start after login), the run
    is retried once automatically. For long runs prefer `start_backtest` + `get_backtest`. Returns `run_id`, `report_path`
    (resolved from `Report=` in the ini), `latest_tester_log` and `journal_notes`/`warnings`;
    if `warnings` mentions `start_time_changed` the tester moved FromDate because history was
    missing, so the report is not comparable with other runs.
    """
    async def _progress(p: float, total: float, message: str) -> None:
        await ctx.report_progress(progress=p, total=total, message=message)

    return await run_backtest_impl(config, wait=wait, timeout_sec=timeout_sec, portable=portable, progress=_progress)


@mcp.tool(annotations=_RUN)
def start_backtest(
    config: Annotated[str, Field(description='Absolute path to the tester.ini file.')],
    timeout_sec: Annotated[int, Field(description='Kill the terminal if it has not exited after this many seconds.')] = 1800,
    portable: Annotated[bool, Field(description='Pass /portable so the terminal uses its install folder as data folder.')] = False,
) -> dict[str, Any]:
    """Launch a backtest in the background and return immediately with a `run_id`.

    Poll `get_backtest(run_id)` until `status` is no longer "running"; the final record carries
    `report_path` and `journal_notes`. Runs are also written to `.mt5tmp/runs/<run_id>.json`
    next to the ini. Only one terminal per install can test at a time.
    """
    return _run_view(_start_run(config, portable, timeout_sec))


@mcp.tool(annotations=_RO)
def get_backtest(
    run_id: Annotated[Optional[str], Field(description='Identifier returned by start_backtest / run_backtest; omit to list every run of this server process.')] = None,
    tail_lines: Annotated[int, Field(description='While running, include this many trailing lines of the tester journal.')] = 20,
) -> dict[str, Any]:
    """Status of a background backtest (running / completed / timeout / cancelled) with report path and journal notes when done; without run_id, lists all runs newest first."""
    if run_id is None:
        runs = [_run_view(r) for r in _runs.list()]
        return {"count": len(runs), "runs": runs}
    try:
        run = _runs.get(run_id)
    except KeyError:
        raise ToolError(f"unknown run_id: {run_id} (call get_backtest without run_id to list runs)")
    return _run_view(run, tail_lines=tail_lines)


@mcp.tool(annotations=_DESTRUCTIVE)
def cancel_backtest(
    run_id: Annotated[str, Field(description='Identifier returned by start_backtest / run_backtest.')],
) -> dict[str, Any]:
    """Kill the terminal of a running background backtest. No report is produced for a cancelled run."""
    try:
        run = _runs.cancel(run_id)
    except KeyError:
        raise ToolError(f"unknown run_id: {run_id}")
    return _run_view(run)


@mcp.tool(annotations=_DESTRUCTIVE)
def kill_terminal(
    all_instances: Annotated[bool, Field(description='false = only terminals launched by this server; true = taskkill every terminal of this edition (may hit a live-trading terminal).')] = False,
) -> dict[str, Any]:
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
def tail_log(
    mode: Annotated[Literal["live", "journal", "terminal", "tester"], Field(description='"live" = MQL5/Files/LiveLog.txt written by the EA, "journal" = MQL5/Logs/YYYYMMDD.log (Experts tab), "terminal" = logs/YYYYMMDD.log (Journal tab: connection, tester start), "tester" = newest Tester/logs file.')] = 'live',
    lines: Annotated[int, Field(description='Number of lines from the end of the file.')] = 100,
    date: Annotated[Optional[str], Field(description='Journal date as YYYYMMDD; defaults to today.')] = None,
    structured: Annotated[bool, Field(description='Parse journal lines into {ts, source, message} records (journal/tester modes).')] = False,
) -> dict[str, Any]:
    """Tail a MetaTrader log: the EA's LiveLog.txt, the daily Experts journal, or the latest tester journal.

    Use mode="tester" right after a backtest to see runtime errors and the effective test period.
    """
    L = layout()
    if mode == "live":
        path = L.files_dir / "LiveLog.txt"
    elif mode in ("journal", "terminal"):
        d = date or datetime.now().strftime("%Y%m%d")
        path = (L.logs_dir if mode == "journal" else L.terminal_logs_dir) / f"{d}.log"
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
    if structured and mode in ("journal", "terminal", "tester"):
        out["records"] = list(iter_journal_lines("\n".join(tail_lines), date=path.stem if path.stem.isdigit() else None))
    else:
        out["content"] = "\n".join(tail_lines)
    return out


@mcp.tool(annotations=_RO)
def list_experts(
    pattern: Annotated[Optional[str], Field(description='Glob pattern; defaults to *.ex5 on MT5 and *.ex4 on MT4.')] = None,
    recurse: Annotated[bool, Field(description='Include subfolders of Experts/.')] = True,
) -> dict[str, Any]:
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
def read_tester_report(
    path: Annotated[Optional[str], Field(description='Absolute path to a report .htm; defaults to the newest report found on disk.')] = None,
    response_format: Annotated[Literal["concise", "detailed"], Field(description='"concise" = summary metrics + counts + report_uri; "detailed" adds the parsed trade rows.')] = "concise",
    max_trades: Annotated[int, Field(description='Max trade rows to return in `trades` when response_format="detailed".')] = 500,
    raw_truncate: Annotated[int, Field(description='Characters of raw HTML to inline (0 = none; read report_uri instead).')] = 0,
) -> dict[str, Any]:
    """Parse a Strategy Tester HTML report (MT5 or MT4) into `summary` metrics; the full HTML stays behind `report_uri`.

    Prefer passing the `report_path` returned by `run_backtest`/`get_backtest`; without `path`
    the newest report on disk is used, which may belong to an older run. `summary` values are
    the strings printed in the report ("10 000.00", "359.61 (3.34%)"); use `compare_reports` /
    `regression_check` for numeric comparison. Read the `mt5://report/{id}` resource only when
    you need the original HTML.
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
    out: dict[str, Any] = {
        "path": str(p),
        "report_uri": _register_report(p),
        "size": len(html),
        "summary": parsed["summary"],
        "trade_rows_detected": parsed["trade_rows_detected"],
    }
    if response_format == "detailed":
        out["trades"] = parsed["trades"]
    if raw_truncate > 0:
        out["raw_truncated"] = html[:raw_truncate]
    return out


@mcp.tool(annotations=_DESTRUCTIVE)
def patch_tester_ini(
    config: Annotated[str, Field(description='Absolute path to the tester.ini file.')],
    updates: Annotated[dict, Field(description='Mapping of "Section.Key" to new value, e.g. {"Tester.Symbol": "EURUSD"}.')],
) -> dict[str, Any]:
    """Set keys in a tester.ini in place, keeping the file's encoding and other lines.

    `updates` maps "Section.Key" to a value, e.g. {"Tester.Symbol": "EURUSD",
    "Tester.FromDate": "2025.01.01", "TesterInputs.RiskPct": "1.5"}. Missing sections/keys are
    added. Run `validate_tester_ini` afterwards; always set Deposit, Currency, Leverage, Model,
    Optimization, Visual, UseLocal/UseRemote/UseCloud and Report explicitly so a run does not
    inherit the machine's last UI state.
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
def compile_and_deploy(
    source: Annotated[str, Field(description='Absolute Windows path to the .mq4/.mq5/.mqh source file.')],
    ea_name: Annotated[Optional[str], Field(description='File name for the binary inside Experts/; defaults to <stem>.ex5.')] = None,
) -> dict[str, Any]:
    """Compile a source and, if it succeeds, copy the fresh binary into the terminal's Experts/ folder.

    Use this before `run_backtest` or `smoke_test`. Returns the `compile` result plus `deploy`;
    `ok` is false if either stage failed (compile errors are in `compile.errors`).
    """
    res = compile(source)
    if not res.get("ok"):
        return {"compile": res, "deploy": None, "ok": False}

    src = Path(source)
    ext = ".ex5" if layout().edition == "mt5" else ".ex4"
    binary = src.with_suffix(ext)
    if not binary.exists():
        return {"compile": res, "deploy": {"error": f"binary not found: {binary}"}, "ok": False}

    deploy_res = deploy(str(binary), name=ea_name)
    return {"compile": res, "deploy": deploy_res, "ok": True}


# ---------------------------------------------------------------------------
# Source analysis
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_DESTRUCTIVE)
def deploy(
    file: Annotated[str, Field(description='Absolute path to a compiled .ex4/.ex5 (goes to Experts/) or a .mqh header (goes to Include/).')],
    name: Annotated[Optional[str], Field(description='File name to use in the target folder; defaults to the source file name.')] = None,
) -> dict[str, Any]:
    """Copy a compiled EA into the terminal's Experts/ folder or a .mqh header into Include/, chosen by extension.

    Overwrites an existing file of the same name. Use `compile_and_deploy` to build and copy in one step.
    """
    L = layout()
    src = Path(file)
    if not src.exists():
        raise ToolError(f"file not found: {src}")
    ext = src.suffix.lower()
    if ext in (".ex4", ".ex5"):
        target_dir = L.experts_dir
    elif ext == ".mqh":
        target_dir = L.include_dir
    else:
        raise ToolError(f"unsupported extension {ext}: expected .ex4/.ex5 (Experts) or .mqh (Include)")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (name or src.name)
    shutil.copy2(src, target)
    return {"copied_to": str(target), "size": target.stat().st_size, "kind": "expert" if ext != ".mqh" else "include"}


@mcp.tool(annotations=_RO)
def inspect_source(
    source: Annotated[Optional[str], Field(description='Absolute path to one .mq4/.mq5/.mqh file (for inputs, includes, docs).')] = None,
    root: Annotated[Optional[str], Field(description='Absolute project folder to scan (for symbol, magic); defaults to the source file folder.')] = None,
    aspects: Annotated[list[Literal["inputs", "includes", "docs", "symbol", "magic"]], Field(description='Which analyses to run; defaults to inputs, includes and docs for a source, plus symbol when `symbol` is given and magic when only `root` is given.')] = [],
    symbol: Annotated[Optional[str], Field(description='Identifier to grep for (whole word, comments and strings skipped); enables the "symbol" aspect.')] = None,
    var_pattern: Annotated[str, Field(description='Substring identifying magic-number variables for the "magic" aspect.')] = "Magic",
    mql_root: Annotated[Optional[str], Field(description='MQL root used to resolve <angle> includes; defaults to the active terminal.')] = None,
    limit: Annotated[int, Field(description='Max symbol matches to return.')] = 200,
) -> dict[str, Any]:
    """Read-only facts about MQL source: `input` declarations, #include tree (with missing files), MetaEditor doc blocks, symbol usages, duplicate magic numbers.

    Replaces extract_inputs / resolve_includes / extract_doc / find_symbol / find_magic_collision.
    """
    if not source and not root:
        raise ToolError("provide `source` (file) and/or `root` (folder)")
    chosen = list(aspects) or (
        [a for a in ("inputs", "includes", "docs") if source]
        + (["symbol"] if symbol else [])
        + (["magic"] if root and not source else [])
    )
    root_p = Path(root) if root else Path(source).parent
    out: dict[str, Any] = {"source": source, "root": str(root_p), "aspects": chosen}
    for a in chosen:
        if a in ("inputs", "includes", "docs") and not source:
            raise ToolError(f"aspect '{a}' needs `source`")
        if a == "inputs":
            out["inputs"] = _analysis.extract_inputs(source)
        elif a == "includes":
            out["includes"] = _analysis.resolve_includes(source, mql_root or str(layout().mql_root))
        elif a == "docs":
            out["docs"] = _analysis.extract_doc(source)
        elif a == "symbol":
            if not symbol:
                raise ToolError("aspect 'symbol' needs `symbol`")
            matches = _analysis.find_symbol(symbol, root_p, limit=limit)
            out["symbol"] = {"name": symbol, "match_count": len(matches), "matches": matches}
        elif a == "magic":
            out["magic"] = _analysis.find_magic_collision(root_p, var_pattern=var_pattern)
    return out


@mcp.tool(annotations=_RO)
def analyze_mql(
    source: Annotated[Optional[str], Field(description='Absolute path to one .mq4/.mq5/.mqh file.')] = None,
    root: Annotated[Optional[str], Field(description='Absolute folder to aggregate metrics over every MQL file (metrics check only).')] = None,
    checks: Annotated[list[Literal["lint", "deprecated", "metrics"]], Field(description='Which checks to run; default all.')] = [],
) -> dict[str, Any]:
    """Static checks on MQL source without compiling: structural lint (missing OnInit/OnTick, unused inputs, hardcoded magic/symbol), MT4-style deprecated API calls with MT5 replacements, and size/nesting metrics.

    Replaces lint_basic / check_deprecated / code_metrics. Use `compile(syntax_only=true)` for real compiler diagnostics.
    """
    if not source and not root:
        raise ToolError("provide `source` (file) or `root` (folder)")
    chosen = list(checks) or (["lint", "deprecated", "metrics"] if source else ["metrics"])
    out: dict[str, Any] = {"source": source, "root": root, "checks": chosen}
    for c in chosen:
        if c == "lint":
            if not source:
                raise ToolError("check 'lint' needs `source`")
            out["lint"] = _raise_if_error(_lint.lint_basic(source))
        elif c == "deprecated":
            if not source:
                raise ToolError("check 'deprecated' needs `source`")
            out["deprecated"] = _lint.check_deprecated(source)
        elif c == "metrics":
            out["metrics"] = _raise_if_error(_analysis.code_metrics(source) if source else _analysis.aggregate_metrics(root))
    out["issue_count"] = len(out.get("lint", {}).get("findings", [])) + len(out.get("deprecated", []))
    return out


@mcp.tool(annotations=_RO)
def read_optimization(
    path: Annotated[Optional[str], Field(description='Absolute path to a Tester/cache/*.opt file; defaults to the newest cache matching expert/symbol/period.')] = None,
    expert: Annotated[Optional[str], Field(description='Expert name (file stem) used to select the matching cache file.')] = None,
    symbol: Annotated[Optional[str], Field(description='Symbol name as shown in Market Watch, e.g. EURUSD.')] = None,
    period: Annotated[Optional[str], Field(description='Timeframe code such as M15, H1, D1.')] = None,
    criterion: Annotated[str, Field(description='Pass field to rank by: profit, profit_factor, expected_payoff, recovery_factor, sharpe_ratio, maxdrawdown, trades, custom_fitness.')] = "profit",
    top_n: Annotated[int, Field(description='How many best passes to return.')] = 10,
    descending: Annotated[bool, Field(description='true = highest first; use false for drawdown-style metrics.')] = True,
) -> dict[str, Any]:
    """Read an MT5 optimisation cache (.opt): header, optimised inputs, pass count and the top N passes by `criterion`.

    Ranking uses every pass in the cache. The best pass of a genetic run is a biased sample;
    prefer robust neighbourhoods over the single top row. Replaces parse_optimization / top_passes.
    """
    L = layout()
    target = path or _optimization.find_latest_opt(L.tester_dir, expert=expert, symbol=symbol, period=period)
    if not target:
        raise ToolError("no matching .opt file found under Tester/ (caches auto-delete after 30 days unused)")
    parsed = _optimization.parse_opt_file(target)
    passes = parsed.pop("passes", None) or []
    if parsed.get("error") and not passes:
        raise ToolError(f"{parsed['error']} ({parsed.get('path', target)})")
    parsed["criterion"] = criterion
    parsed["top"] = _optimization.top_passes(passes, criterion=criterion, n=top_n, descending=descending)
    return parsed


@mcp.tool(annotations=_RO)
def compare_reports(
    baseline: Annotated[str, Field(description='Absolute path to the baseline tester report (.htm).')],
    candidate: Annotated[str, Field(description='Absolute path to the candidate tester report (.htm).')],
    guards: Annotated[Optional[dict], Field(description='Optional percent thresholds per summary key, e.g. {"net_profit": -5, "profit_factor": -10, "max_drawdown": 25}; when given, `violations` and `ok` are added.')] = None,
) -> dict[str, Any]:
    """Diff two tester reports key by key (absolute and percent deltas), optionally checking guard thresholds.

    With `guards`, a profit-style metric below its threshold or a drawdown/loss metric above it is a
    violation. Accepting only improvements across many iterations is selection bias; treat guards as a
    sanity gate, not proof of robustness. Replaces regression_check.
    """
    out = _raise_if_error(_reports.compare_reports(baseline, candidate))
    if guards is not None:
        reg = _reports.regression_check(baseline, candidate, guards=guards)
        out.update({"guards": reg["guards"], "violations": reg["violations"], "ok": reg["ok"]})
    return out


@mcp.tool(annotations=_DESTRUCTIVE)
def gen_tester_inputs(
    source: Annotated[str, Field(description='Absolute Windows path to the .mq4/.mq5/.mqh source file.')],
    write_to: Annotated[Optional[str], Field(description='Optional tester.ini path; when given the [TesterInputs] block is written into it.')] = None,
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Lint / validation
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_RO)
def validate_tester_ini(
    config: Annotated[str, Field(description='Absolute path to the tester.ini file.')],
    source: Annotated[Optional[str], Field(description='Optional EA source; when given, [TesterInputs] keys are cross-checked against its input declarations.')] = None,
) -> dict[str, Any]:
    """Sanity-check a tester.ini. If `source` given, cross-check inputs vs EA declarations."""
    return _raise_if_error(_lint.validate_tester_ini(config, source=source))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_DESTRUCTIVE)
def format_mql(
    source: Annotated[str, Field(description='Absolute Windows path to the .mq4/.mq5/.mqh source file.')],
    style: Annotated[Optional[str], Field(description='clang-format style string; defaults to an MQL-friendly LLVM-based profile.')] = None,
    write: Annotated[bool, Field(description='false = report/diff only (default); true = overwrite the file in its original encoding.')] = True,
) -> dict[str, Any]:
    """Format an MQL file with clang-format (MQL literals such as D'…' and `input group` lines are protected).

    Default is a dry run reporting whether the file would change; pass `write=true` to apply.
    Replaces format_check.
    """
    return _raise_if_error(_formatting.format_mql(source, style=style, write=write))


# ---------------------------------------------------------------------------
# Refactor
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_DESTRUCTIVE)
def rename_symbol(
    old: Annotated[str, Field(description='Identifier to rename (whole-word match).')],
    new: Annotated[str, Field(description='New identifier.')],
    root: Annotated[str, Field(description='Absolute path to the project folder to scan recursively for MQL files.')],
    dry_run: Annotated[bool, Field(description='true = preview the change and write nothing; false = apply it to the files.')] = True,
) -> dict[str, Any]:
    """Rename a symbol across MQL files (whole-word match). `dry_run=True` previews only."""
    return _raise_if_error(_refactor.rename_symbol(old, new, root, dry_run=dry_run))


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reports comparison
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Source snapshots
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_WRITE)
def snapshot_sources(
    sources: Annotated[list[str], Field(description='Absolute paths of the source files to freeze.')],
    dest: Annotated[str, Field(description='Absolute path to the folder that holds snapshots.')],
    label: Annotated[Optional[str], Field(description='Folder name for this snapshot; defaults to a timestamp.')] = None,
) -> dict[str, Any]:
    """Freeze a copy of source files into a timestamped folder under `dest`."""
    return _snapshot.snapshot_sources(sources, dest, label=label)


@mcp.tool(annotations=_RO)
def list_snapshots(
    dest: Annotated[str, Field(description='Absolute path to the folder that holds snapshots.')],
) -> dict[str, Any]:
    """List all snapshot folders under `dest`."""
    return {"snapshots": _snapshot.list_snapshots(dest)}


# ---------------------------------------------------------------------------
# Terminal selection
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_RUN)
def select_terminal(
    origin: Annotated[Optional[str], Field(description="Install path stored in the terminal's origin.txt, e.g. C:\\\\Program Files\\\\MetaTrader 5.")] = None,
    hash: Annotated[Optional[str], Field(description='32-character terminal data folder name under %APPDATA%\\\\MetaQuotes\\\\Terminal.')] = None,
    install: Annotated[Optional[str], Field(description='Install folder containing terminal64.exe; its data folder is found by scanning origin.txt files.')] = None,
    edition: Annotated[str, Field(description='"mt5" or "mt4".')] = 'mt5',
) -> dict[str, Any]:
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
def smoke_test(
    source: Annotated[str, Field(description='Absolute Windows path to the .mq4/.mq5/.mqh source file.')],
    expert_name: Annotated[Optional[str], Field(description="Name to use in Expert= of the generated ini; defaults to the binary's stem.")] = None,
    symbol: Annotated[str, Field(description='Symbol name as shown in Market Watch, e.g. EURUSD.')] = 'EURUSD',
    period: Annotated[str, Field(description='Timeframe code such as M15, H1, D1.')] = 'M15',
    days: Annotated[int, Field(description='Length of the backtest window in days (ends 2 days ago).')] = 1,
    timeout_sec: Annotated[int, Field(description='Give up after this many seconds.')] = 600,
) -> dict[str, Any]:
    """One-call health check for an EA: compile, deploy, run a 1-day headless backtest, scan the journal.

    Catches problems that compile cleanly but fail at runtime (OnInit errors, array out of range,
    divide by zero). `ok` is true only if every stage passes; `stage` tells where it stopped.
    Overwrites the EA of the same name in Experts/. Takes 1-10 minutes.
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
def extract_function(
    source: Annotated[str, Field(description='Absolute Windows path to the .mq4/.mq5/.mqh source file.')],
    line_start: Annotated[int, Field(description='First line (1-based, inclusive) of the block to extract.')],
    line_end: Annotated[int, Field(description='Last line (1-based, inclusive) of the block to extract.')],
    new_name: Annotated[str, Field(description='Name of the new helper function.')],
    return_type: Annotated[str, Field(description='Return type of the helper, e.g. void or double.')] = 'void',
    target_file: Annotated[Optional[str], Field(description='Optional .mqh path to append the helper to; defaults to inserting above the enclosing function.')] = None,
    dry_run: Annotated[bool, Field(description='true = preview the change and write nothing; false = apply it to the files.')] = True,
) -> dict[str, Any]:
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

@mcp.resource("mt5://report/{report_id}")
def report_resource(report_id: str) -> str:
    """Full HTML of a tester report referenced by `report_uri` from read_tester_report / get_backtest; "latest" = newest on disk."""
    if report_id == "latest":
        reports = _find_reports(layout())
        if not reports:
            return "(no tester reports found)"
        return read_text_auto(reports[0])
    p = _report_ids.get(report_id)
    if p is None or not p.exists():
        return f"(unknown or expired report id {report_id}; call read_tester_report again)"
    return read_text_auto(p)


# ---------------------------------------------------------------------------
# Deprecated aliases (0.5.x): the pre-0.5.0 tool names, forwarding to the consolidated tools.
# They are removed in 0.6.0. Set MCP_MT5_LEGACY_TOOLS=0 to hide them and save context.
# ---------------------------------------------------------------------------

LEGACY_TOOLS_ENABLED = os.environ.get("MCP_MT5_LEGACY_TOOLS", "1").lower() not in ("0", "false", "no", "off")
LEGACY_TOOL_NAMES = (
    "syntax_check", "deploy_ea", "install_include", "extract_inputs", "resolve_includes", "extract_doc",
    "find_symbol", "find_magic_collision", "lint_basic", "check_deprecated", "code_metrics", "format_check",
    "parse_optimization", "top_passes", "regression_check", "list_backtests",
)
_SRC = Annotated[str, Field(description="Absolute Windows path to the .mq4/.mq5/.mqh source file.")]
_ROOT = Annotated[str, Field(description="Absolute path to the project folder to scan recursively.")]

if LEGACY_TOOLS_ENABLED:

    @mcp.tool(annotations=_RUN)
    def syntax_check(source: _SRC, timeout_sec: Annotated[int, Field(description="Give up after this many seconds.")] = 60) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use compile(source, syntax_only=true)."""
        return compile(source, timeout_sec=timeout_sec, syntax_only=True)

    @mcp.tool(annotations=_DESTRUCTIVE)
    def deploy_ea(source_ex: Annotated[str, Field(description="Absolute path to the compiled .ex4/.ex5 binary.")],
                  name: Annotated[Optional[str], Field(description="File name to use inside Experts/.")] = None) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use deploy(file, name)."""
        return deploy(source_ex, name)

    @mcp.tool(annotations=_DESTRUCTIVE)
    def install_include(source: _SRC, target_name: Annotated[Optional[str], Field(description="File name to use inside Include/.")] = None) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use deploy(file, name) with a .mqh file."""
        return deploy(source, target_name)

    @mcp.tool(annotations=_RO)
    def extract_inputs(source: _SRC) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use inspect_source(source, aspects=["inputs"])."""
        return {"file": source, "inputs": inspect_source(source, aspects=["inputs"])["inputs"]}

    @mcp.tool(annotations=_RO)
    def resolve_includes(source: _SRC, mql_root: Annotated[Optional[str], Field(description="MQL root for <angle> includes.")] = None) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use inspect_source(source, aspects=["includes"])."""
        return inspect_source(source, aspects=["includes"], mql_root=mql_root)["includes"]

    @mcp.tool(annotations=_RO)
    def extract_doc(source: _SRC) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use inspect_source(source, aspects=["docs"])."""
        return {"file": source, "blocks": inspect_source(source, aspects=["docs"])["docs"]}

    @mcp.tool(annotations=_RO)
    def find_symbol(symbol: Annotated[str, Field(description="Identifier to search for.")], root: _ROOT,
                    exts: Annotated[Optional[list[str]], Field(description="File extensions to search.")] = None,
                    limit: Annotated[int, Field(description="Stop after this many matches.")] = 200) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use inspect_source(root=..., symbol=...)."""
        matches = _analysis.find_symbol(symbol, root, exts=tuple(exts) if exts else (".mq4", ".mq5", ".mqh"), limit=limit)
        return {"symbol": symbol, "root": root, "match_count": len(matches), "matches": matches}

    @mcp.tool(annotations=_RO)
    def find_magic_collision(root: _ROOT, var_pattern: Annotated[str, Field(description="Substring identifying magic-number variables.")] = "Magic") -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use inspect_source(root=..., aspects=["magic"])."""
        return inspect_source(root=root, aspects=["magic"], var_pattern=var_pattern)["magic"]

    @mcp.tool(annotations=_RO)
    def lint_basic(source: _SRC) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use analyze_mql(source, checks=["lint"])."""
        return analyze_mql(source, checks=["lint"])["lint"]

    @mcp.tool(annotations=_RO)
    def check_deprecated(source: _SRC) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use analyze_mql(source, checks=["deprecated"])."""
        return {"file": source, "findings": analyze_mql(source, checks=["deprecated"])["deprecated"]}

    @mcp.tool(annotations=_RO)
    def code_metrics(source: Annotated[Optional[str], Field(description="Absolute path of one source file.")] = None,
                     root: Annotated[Optional[str], Field(description="Absolute folder to aggregate over.")] = None) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use analyze_mql(source or root, checks=["metrics"])."""
        return analyze_mql(source=source, root=root, checks=["metrics"])["metrics"]

    @mcp.tool(annotations=_RO)
    def format_check(source: _SRC, style: Annotated[Optional[str], Field(description="clang-format style string.")] = None) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use format_mql(source) (dry run by default)."""
        return format_mql(source, style=style, write=False)

    @mcp.tool(annotations=_RO)
    def parse_optimization(path: Annotated[Optional[str], Field(description="Absolute path to a .opt cache.")] = None,
                           expert: Annotated[Optional[str], Field(description="Expert name to select the cache.")] = None,
                           symbol: Annotated[Optional[str], Field(description="Symbol to select the cache.")] = None,
                           period: Annotated[Optional[str], Field(description="Timeframe to select the cache.")] = None,
                           sample: Annotated[int, Field(description="Passes to include in passes_sample.")] = 50) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use read_optimization(...)."""
        out = read_optimization(path=path, expert=expert, symbol=symbol, period=period, top_n=sample)
        out["passes_sample"] = out.pop("top")
        return out

    @mcp.tool(annotations=_RO)
    def top_passes(opt_path: Annotated[Optional[str], Field(description="Absolute path to a .opt cache.")] = None,
                   criterion: Annotated[str, Field(description="Pass field to rank by.")] = "profit",
                   n: Annotated[int, Field(description="How many passes to return.")] = 10,
                   descending: Annotated[bool, Field(description="true = highest first.")] = True,
                   expert: Annotated[Optional[str], Field(description="Expert name to select the cache.")] = None,
                   symbol: Annotated[Optional[str], Field(description="Symbol to select the cache.")] = None,
                   period: Annotated[Optional[str], Field(description="Timeframe to select the cache.")] = None) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use read_optimization(criterion=..., top_n=...)."""
        return read_optimization(path=opt_path, expert=expert, symbol=symbol, period=period, criterion=criterion, top_n=n, descending=descending)

    @mcp.tool(annotations=_RO)
    def regression_check(baseline: Annotated[str, Field(description="Absolute path to the baseline report.")],
                         candidate: Annotated[str, Field(description="Absolute path to the candidate report.")],
                         guards: Annotated[Optional[dict], Field(description="Percent thresholds per summary key.")] = None) -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use compare_reports(baseline, candidate, guards=...)."""
        return _raise_if_error(_reports.regression_check(baseline, candidate, guards=guards))

    @mcp.tool(annotations=_RO)
    def list_backtests() -> dict[str, Any]:
        """DEPRECATED (removed in 0.6.0): use get_backtest() without run_id."""
        return get_backtest()


@mcp.resource("mt5://log/{mode}")
def log_resource(mode: str) -> str:
    """Latest 500 lines of a MetaTrader log: mode = livelog (MQL5/Files/LiveLog.txt), journal (today's MQL5/Logs), terminal (today's logs/), tester (newest Tester/logs)."""
    L = layout()
    if mode == "livelog":
        path = L.files_dir / "LiveLog.txt"
    elif mode == "journal":
        path = L.logs_dir / f"{datetime.now().strftime('%Y%m%d')}.log"
    elif mode == "terminal":
        path = L.terminal_logs_dir / f"{datetime.now().strftime('%Y%m%d')}.log"
    elif mode == "tester":
        files = sorted(L.tester_logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) if L.tester_logs.exists() else []
        if not files:
            return "(no tester logs)"
        path = files[0]
    else:
        return f"(unknown log mode {mode}; use livelog, journal, terminal or tester)"
    if not path.exists():
        return f"(no log at {path})"
    return f"# {path.name}\n" + "\n".join(read_text_auto(path).splitlines()[-500:])


def main():
    mcp.run()


if __name__ == "__main__":
    main()
