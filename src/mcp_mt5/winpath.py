"""Translate paths for the MetaTrader binaries when the server itself runs under WSL.

MetaEditor64.exe and terminal64.exe only understand Windows paths. When this server runs
inside WSL (Linux) and is pointed at an install under /mnt/<drive>/..., every path passed on
the command line must be converted with `wslpath -w`. Python-side checks keep using the
Linux view of the same files. On native Windows this is a no-op.
"""
from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from pathlib import Path


def is_wsl_path(p: str | Path) -> bool:
    s = str(p)
    return sys.platform != "win32" and s.startswith("/mnt/") and len(s) > 6 and s[5].isalpha() and s[6] == "/"


@functools.lru_cache(maxsize=1024)
def _wslpath_w(s: str) -> str:
    if not shutil.which("wslpath"):
        return s
    try:
        out = subprocess.run(["wslpath", "-w", s], capture_output=True, text=True, check=True, timeout=10)
        return out.stdout.strip() or s
    except (OSError, subprocess.SubprocessError):
        return s


def win_path(p: str | Path) -> str:
    """Path as a Windows binary must receive it (unchanged unless we are on WSL under /mnt/)."""
    s = str(p)
    return _wslpath_w(s) if is_wsl_path(s) else s
