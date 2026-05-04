"""Strategy Tester optimization: launch + parse `.opt` results."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional


def parse_opt_file(path: str | Path, max_passes: int = 5000) -> dict:
    """Best-effort parser for an MT5 `.opt` (optimization passes) file.

    The `.opt` binary format is undocumented; this reader extracts a sequence of
    {pass_id, profit, expected_payoff, profit_factor, recovery, sharpe, custom,
    drawdown, total_trades} records and falls back to a header-only summary if
    the layout doesn't match.
    """
    p = Path(path)
    if not p.exists():
        return {"error": f"not found: {p}"}

    raw = p.read_bytes()
    if len(raw) < 64:
        return {"error": "file too small to be a valid .opt", "size": len(raw)}

    header_magic = raw[:4]
    record_size_candidates = [128, 96, 80, 64]
    passes: list[dict] = []

    for rec_size in record_size_candidates:
        body = raw[64:]
        if len(body) % rec_size != 0:
            continue
        count = len(body) // rec_size
        if count == 0 or count > max_passes * 4:
            continue
        attempt: list[dict] = []
        ok = True
        for i in range(min(count, max_passes)):
            chunk = body[i * rec_size : (i + 1) * rec_size]
            try:
                pass_id = struct.unpack_from("<i", chunk, 0)[0]
                profit = struct.unpack_from("<d", chunk, 8)[0]
                drawdown = struct.unpack_from("<d", chunk, 16)[0]
                expected_payoff = struct.unpack_from("<d", chunk, 24)[0]
                profit_factor = struct.unpack_from("<d", chunk, 32)[0]
                trades = struct.unpack_from("<i", chunk, 40)[0]
            except struct.error:
                ok = False
                break
            if abs(profit) > 1e12 or trades < 0 or trades > 1_000_000:
                ok = False
                break
            attempt.append({
                "pass_id": pass_id,
                "profit": profit,
                "drawdown": drawdown,
                "expected_payoff": expected_payoff,
                "profit_factor": profit_factor,
                "trades": trades,
            })
        if ok and attempt:
            passes = attempt
            return {
                "path": str(p),
                "magic": header_magic.hex(),
                "record_size": rec_size,
                "pass_count": len(passes),
                "passes_sample": passes[:50],
            }

    return {
        "path": str(p),
        "magic": header_magic.hex(),
        "size": len(raw),
        "warning": "could not parse passes — file format may differ; raw size returned",
    }


def top_passes(passes: list[dict], criterion: str = "profit", n: int = 10,
               descending: bool = True) -> list[dict]:
    """Sort optimization passes by criterion and return the top N."""
    if not passes:
        return []
    if criterion not in passes[0]:
        return []
    return sorted(passes, key=lambda r: r.get(criterion, 0), reverse=descending)[:n]


def find_latest_opt(tester_dir: str | Path) -> Optional[str]:
    p = Path(tester_dir)
    if not p.exists():
        return None
    files = sorted(p.rglob("*.opt"), key=lambda f: f.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None
