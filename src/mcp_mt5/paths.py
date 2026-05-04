"""Resolve MetaTrader install + terminal data paths."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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


def find_terminal_for_install(install: Path) -> Optional[tuple[str, Path]]:
    """Scan %APPDATA%\\MetaQuotes\\Terminal\\* for the data folder owned by `install`.

    Returns (hash, data_dir) or None.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "MetaQuotes" / "Terminal"
    if not base.exists():
        return None

    target = str(install).strip().lower()
    for child in base.iterdir():
        if not child.is_dir() or len(child.name) != 32:
            continue
        origin = _read_origin(child)
        if origin and origin.strip().lower() == target:
            return child.name, child
    return None


def detect_layout(
    install: Optional[str] = None,
    data: Optional[str] = None,
    terminal_hash: Optional[str] = None,
    edition: str = "mt5",
) -> MT5Layout:
    """Resolve layout from explicit args, env, then auto-scan."""
    install_p = Path(install or os.environ.get("MT5_INSTALL")
                     or (r"C:\Program Files\MetaTrader 5" if edition == "mt5" else r"C:\Program Files\MetaTrader 4"))

    edition_env = os.environ.get("MT5_EDITION", edition)
    if edition_env in ("mt4", "mt5"):
        edition = edition_env

    # Explicit data path wins
    if data:
        data_p = Path(data)
        h = terminal_hash or os.environ.get("MT5_TERMINAL_HASH") or data_p.name
        return MT5Layout(install=install_p, data=data_p, terminal_hash=h, edition=edition)

    # Explicit hash via env or arg
    h = terminal_hash or os.environ.get("MT5_TERMINAL_HASH")
    if h:
        appdata = os.environ.get("APPDATA")
        if appdata:
            data_p = Path(appdata) / "MetaQuotes" / "Terminal" / h
            return MT5Layout(install=install_p, data=data_p, terminal_hash=h, edition=edition)

    # Auto-scan origin.txt
    found = find_terminal_for_install(install_p)
    if found:
        h, data_p = found
        return MT5Layout(install=install_p, data=data_p, terminal_hash=h, edition=edition)

    # Fallback — install dir itself (portable mode)
    return MT5Layout(install=install_p, data=install_p, terminal_hash="portable", edition=edition)
