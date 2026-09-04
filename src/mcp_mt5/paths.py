"""Resolve MetaTrader install + terminal data paths."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .winpath import win_path

_MNT_ROOT = Path("/mnt")  # WSL mount root; tests point this at a temp folder


@dataclass(frozen=True)
class MT5Layout:
    install: Path
    data: Path
    terminal_hash: str
    edition: str  # "mt5" or "mt4"

    @property
    def metaeditor(self) -> Path:
        if self.edition == "mt5":
            return self.install / "MetaEditor64.exe"
        # MT4 uses metaeditor.exe (32-bit by default)
        for cand in ("metaeditor.exe", "MetaEditor.exe", "MetaEditor64.exe"):
            p = self.install / cand
            if p.exists():
                return p
        return self.install / "metaeditor.exe"

    @property
    def terminal(self) -> Path:
        if self.edition == "mt5":
            return self.install / "terminal64.exe"
        for cand in ("terminal.exe", "terminal64.exe"):
            p = self.install / cand
            if p.exists():
                return p
        return self.install / "terminal.exe"

    @property
    def mql_root(self) -> Path:
        return self.data / ("MQL5" if self.edition == "mt5" else "MQL4")

    @property
    def include_dir(self) -> Path:
        return self.mql_root / "Include"

    @property
    def experts_dir(self) -> Path:
        return self.mql_root / "Experts"

    @property
    def files_dir(self) -> Path:
        return self.mql_root / "Files"

    @property
    def logs_dir(self) -> Path:
        return self.mql_root / "Logs"

    @property
    def terminal_logs_dir(self) -> Path:
        """The terminal's own Journal tab log (connection, tester launches), distinct from MQL5/Logs (Experts tab)."""
        return self.data / "logs"

    @property
    def tester_logs(self) -> Path:
        return self.data / "Tester" / "logs"

    @property
    def tester_dir(self) -> Path:
        return self.data / "Tester"

    def issues(self) -> list[str]:
        out = []
        for name, p in [
            ("MetaEditor binary", self.metaeditor),
            ("terminal binary", self.terminal),
            ("MQL root", self.mql_root),
            ("Experts dir", self.experts_dir),
        ]:
            if not p.exists():
                out.append(f"missing {name}: {p}")
        return out


def _read_origin(terminal_dir: Path) -> Optional[str]:
    f = terminal_dir / "origin.txt"
    if not f.exists():
        return None
    try:
        raw = f.read_bytes()
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return raw.decode("utf-16", errors="replace").strip()
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def terminal_roots() -> list[Path]:
    """Folders that hold `<hash>/origin.txt` terminal data dirs.

    Windows: `%APPDATA%\\MetaQuotes\\Terminal`. WSL: the same folder of every Windows user
    profile reachable under `/mnt/<drive>/Users/*` (APPDATA is not set inside WSL).
    """
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "MetaQuotes" / "Terminal")
    elif sys.platform != "win32":                  # WSL: APPDATA is unset, look through the Windows profiles
        for drive in sorted(_MNT_ROOT.glob("[a-z]")) if _MNT_ROOT.exists() else []:
            users = drive / "Users"
            if users.is_dir():
                for user in users.iterdir():
                    cand = user / "AppData" / "Roaming" / "MetaQuotes" / "Terminal"
                    if cand.is_dir():
                        roots.append(cand)
    return [r for r in roots if r.exists()]


def _iter_terminal_dirs():
    for base in terminal_roots():
        for child in sorted(base.iterdir()):
            if child.is_dir() and len(child.name) in (16, 32):   # MT5 uses 32 chars, MT4 16
                yield child


def _same_install(origin: str, install: Path) -> bool:
    """`origin.txt` always holds a Windows path; compare against both views of `install`."""
    o = origin.strip().lower().rstrip("\\/")
    candidates = {str(install).strip().lower().rstrip("\\/"), win_path(install).strip().lower().rstrip("\\/")}
    return o in candidates


def find_terminal_for_install(install: Path) -> Optional[tuple[str, Path]]:
    """Find the terminal data folder whose origin.txt points at `install`. Returns (hash, data_dir) or None."""
    for child in _iter_terminal_dirs():
        origin = _read_origin(child)
        if origin and _same_install(origin, install):
            return child.name, child
    return None


def list_terminal_origins() -> list[dict]:
    """Enumerate all MetaTrader terminal data folders with their origin install path."""
    return [{"hash": d.name, "origin": _read_origin(d), "data_dir": str(d)} for d in _iter_terminal_dirs()]


def default_install(edition: str) -> Path:
    """`C:\\Program Files\\MetaTrader 5` on Windows; its `/mnt/c/...` view when running under WSL."""
    name = "MetaTrader 5" if edition == "mt5" else "MetaTrader 4"
    if sys.platform != "win32":
        for drive in sorted(_MNT_ROOT.glob("[a-z]")) if _MNT_ROOT.exists() else []:
            cand = drive / "Program Files" / name
            if cand.exists():
                return cand
    return Path(rf"C:\Program Files\{name}")


def detect_layout(
    install: Optional[str] = None,
    data: Optional[str] = None,
    terminal_hash: Optional[str] = None,
    edition: str = "mt5",
) -> MT5Layout:
    """Resolve layout from explicit args, env, then auto-scan."""
    edition_env = os.environ.get("MT5_EDITION", edition)
    if edition_env in ("mt4", "mt5"):
        edition = edition_env

    explicit_install = install or os.environ.get("MT5_INSTALL")
    install_p = Path(explicit_install) if explicit_install else default_install(edition)

    # Explicit data path wins (argument, then MT5_DATA env var)
    data = data or os.environ.get("MT5_DATA")
    if data:
        data_p = Path(data)
        h = terminal_hash or os.environ.get("MT5_TERMINAL_HASH") or data_p.name
        return MT5Layout(install=install_p, data=data_p, terminal_hash=h, edition=edition)

    # Explicit hash via env or arg
    h = terminal_hash or os.environ.get("MT5_TERMINAL_HASH")
    if h:
        for base in terminal_roots():
            if (base / h).is_dir():
                return MT5Layout(install=install_p, data=base / h, terminal_hash=h, edition=edition)

    # Auto-scan origin.txt
    found = find_terminal_for_install(install_p)
    if found:
        h, data_p = found
        return MT5Layout(install=install_p, data=data_p, terminal_hash=h, edition=edition)

    # Fallback — install dir itself (portable mode)
    return MT5Layout(install=install_p, data=install_p, terminal_hash="portable", edition=edition)
