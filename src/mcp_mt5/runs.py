"""Background backtest runs: launch the terminal, track it, persist the outcome.

A run is started with `RunRegistry.start()`, which spawns the terminal detached and a
watcher thread that waits for it (or kills it at the timeout) and then calls the
`finalize` callback to collect the report path and journal notes. Each run is also
written as JSON under `<workdir>/runs/<run_id>.json` so results survive a server restart.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import smoke as _smoke
from .workdir import workdir

Finalizer = Callable[["Run"], dict]


@dataclass
class Run:
    run_id: str
    config: str
    cmd: list[str]
    pid: Optional[int] = None
    status: str = "running"          # running | completed | timeout | cancelled | failed
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    returncode: Optional[int] = None
    timeout_sec: int = 1800
    result: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def elapsed_sec(self) -> float:
        end = self.finished or time.time()
        return round(end - self.started, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["elapsed_sec"] = self.elapsed_sec
        return d


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # -- persistence ----------------------------------------------------------------
    @staticmethod
    def _store(run: Run) -> Path:
        d = workdir(Path(run.config)) / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{run.run_id}.json"

    def _save(self, run: Run) -> None:
        try:
            self._store(run).write_text(json.dumps(run.to_dict(), indent=2, default=str), encoding="utf-8")
        except OSError:
            pass

    # -- lifecycle -------------------------------------------------------------------
    def start(self, terminal: Path, config: Path, timeout_sec: int, extra_args: tuple[str, ...],
              finalize: Finalizer) -> Run:
        cmd = [str(terminal), f"/config:{config}", *extra_args]
        run = Run(run_id=uuid.uuid4().hex[:12], config=str(config), cmd=cmd, timeout_sec=timeout_sec)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            run.status, run.error, run.finished = "failed", str(e), time.time()
            with self._lock:
                self._runs[run.run_id] = run
            self._save(run)
            return run
        run.pid = proc.pid
        _smoke.spawned_pids.add(proc.pid)
        with self._lock:
            self._runs[run.run_id] = run
            self._procs[run.run_id] = proc
        self._save(run)
        threading.Thread(target=self._watch, args=(run, proc, finalize), daemon=True).start()
        return run

    def _watch(self, run: Run, proc: subprocess.Popen, finalize: Finalizer) -> None:
        try:
            run.returncode = proc.wait(timeout=run.timeout_sec)
            if run.status == "running":
                run.status = "completed"
        except subprocess.TimeoutExpired:
            proc.kill()
            run.status = "timeout"
            run.returncode = -1
        finally:
            run.finished = time.time()
            _smoke.spawned_pids.discard(proc.pid)
            with self._lock:
                self._procs.pop(run.run_id, None)
            try:
                run.result = finalize(run)
            except Exception as e:  # pragma: no cover - defensive
                run.error = f"finalize failed: {e}"
            self._save(run)

    def cancel(self, run_id: str) -> Run:
        run = self.get(run_id)
        with self._lock:
            proc = self._procs.get(run_id)
        if proc is not None and proc.poll() is None:
            run.status = "cancelled"
            proc.kill()
        return run

    # -- queries ---------------------------------------------------------------------
    def get(self, run_id: str) -> Run:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def list(self) -> list[Run]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r.started, reverse=True)

    def load_persisted(self, config_dir: Path) -> list[dict]:
        """Runs recorded on disk for configs under `config_dir` (from this or earlier server processes)."""
        out: list[dict] = []
        runs_dir = workdir(config_dir / "_") / "runs"
        if runs_dir.exists():
            for f in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    out.append(json.loads(f.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return out


registry = RunRegistry()
