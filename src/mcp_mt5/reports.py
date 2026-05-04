"""Backtest report comparison + regression detection."""
from __future__ import annotations

from pathlib import Path

from .parsers import parse_tester_report, read_text_auto


def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", "").replace("%", "").strip())
    except (ValueError, AttributeError):
        return None


def compare_reports(baseline: str | Path, candidate: str | Path) -> dict:
    """Diff two MT5 tester HTML reports key-by-key."""
    bp = Path(baseline)
    cp = Path(candidate)
    if not bp.exists():
        return {"error": f"baseline missing: {bp}"}
    if not cp.exists():
        return {"error": f"candidate missing: {cp}"}

    base = parse_tester_report(read_text_auto(bp))["summary"]
    cand = parse_tester_report(read_text_auto(cp))["summary"]

    keys = set(base) | set(cand)
    diffs: list[dict] = []
    for k in sorted(keys):
        bv = base.get(k)
        cv = cand.get(k)
        bf = _to_float(bv)
        cf = _to_float(cv)
        delta = None
        pct = None
        if bf is not None and cf is not None:
            delta = round(cf - bf, 6)
            pct = round((delta / bf) * 100, 4) if bf != 0 else None
        diffs.append({"key": k, "baseline": bv, "candidate": cv, "delta": delta, "pct": pct})

    return {
        "baseline": str(bp),
        "candidate": str(cp),
        "baseline_summary": base,
        "candidate_summary": cand,
        "diffs": diffs,
    }


def regression_check(baseline: str | Path, candidate: str | Path,
                     guards: dict[str, float] | None = None) -> dict:
    """Detect regressions vs guard thresholds.

    `guards` is a mapping of summary key → minimum acceptable percentage delta vs baseline.
    Example: `{"net_profit": -5, "profit_factor": -10}` means the candidate may not be
    more than 5% worse than baseline net profit, or 10% worse on profit factor.
    """
    cmp = compare_reports(baseline, candidate)
    if "error" in cmp:
        return cmp

    guards = guards or {"net_profit": -5.0, "profit_factor": -10.0, "max_drawdown": 25.0}
    violations: list[dict] = []
    for d in cmp["diffs"]:
        key = d["key"]
        if key not in guards or d["pct"] is None:
            continue
        threshold = guards[key]
        # For drawdown / loss style metrics, an *increase* (positive pct) is bad.
        # Otherwise a *decrease* below threshold is bad.
        if "drawdown" in key or "loss" in key:
            if d["pct"] > threshold:
                violations.append({"key": key, "pct": d["pct"], "threshold": threshold,
                                   "kind": "regression"})
        else:
            if d["pct"] < threshold:
                violations.append({"key": key, "pct": d["pct"], "threshold": threshold,
                                   "kind": "regression"})

    return {
        "baseline": cmp["baseline"],
        "candidate": cmp["candidate"],
        "guards": guards,
        "violations": violations,
        "ok": len(violations) == 0,
    }
