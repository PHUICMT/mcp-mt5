"""Tests for refactor, optimization, reports, snapshot modules."""
from pathlib import Path

from mcp_mt5.refactor import rename_symbol
from mcp_mt5.reports import compare_reports, regression_check
from mcp_mt5.snapshot import snapshot_sources, list_snapshots
from mcp_mt5.optimization import top_passes


def test_rename_symbol_dry_run(tmp_path: Path):
    f = tmp_path / "a.mq5"
    f.write_text("int InpRisk = 1; double risk = InpRisk;", encoding="utf-8")
    out = rename_symbol("InpRisk", "InpRiskPercent", tmp_path, dry_run=True)
    assert out["files_changed"] == 1
    assert out["total_replacements"] == 2
    # not actually written
    assert "InpRiskPercent" not in f.read_text(encoding="utf-8")


def test_rename_symbol_writes(tmp_path: Path):
    f = tmp_path / "a.mq5"
    f.write_text("int InpRisk = 1;\n", encoding="utf-8")
    rename_symbol("InpRisk", "InpRiskPct", tmp_path, dry_run=False)
    assert "InpRiskPct" in f.read_text(encoding="utf-8")


REPORT_HTML = """
<html><body><table>
<tr><td>Total Net Profit:</td><td>{profit}</td></tr>
<tr><td>Profit Factor:</td><td>{pf}</td></tr>
<tr><td>Maximal Drawdown:</td><td>{dd}</td></tr>
</table></body></html>
"""


def test_compare_reports_diffs(tmp_path: Path):
    base = tmp_path / "base.htm"
    cand = tmp_path / "cand.htm"
    base.write_text(REPORT_HTML.format(profit="1000.00", pf="1.50", dd="200.00"), encoding="utf-8")
    cand.write_text(REPORT_HTML.format(profit="1200.00", pf="1.65", dd="180.00"), encoding="utf-8")
    out = compare_reports(base, cand)
    profit_diff = next(d for d in out["diffs"] if d["key"] == "net_profit")
    assert profit_diff["delta"] == 200.0
    assert profit_diff["pct"] == 20.0


def test_regression_check_passes(tmp_path: Path):
    base = tmp_path / "base.htm"
    cand = tmp_path / "cand.htm"
    base.write_text(REPORT_HTML.format(profit="1000.00", pf="1.50", dd="200.00"), encoding="utf-8")
    cand.write_text(REPORT_HTML.format(profit="950.00", pf="1.45", dd="210.00"), encoding="utf-8")
    out = regression_check(base, cand, guards={"net_profit": -10})
    assert out["ok"] is True


def test_regression_check_fails(tmp_path: Path):
    base = tmp_path / "base.htm"
    cand = tmp_path / "cand.htm"
    base.write_text(REPORT_HTML.format(profit="1000.00", pf="1.50", dd="200.00"), encoding="utf-8")
    cand.write_text(REPORT_HTML.format(profit="500.00", pf="1.10", dd="500.00"), encoding="utf-8")
    out = regression_check(base, cand, guards={"net_profit": -10})
    assert out["ok"] is False
    assert any(v["key"] == "net_profit" for v in out["violations"])


def test_snapshot_sources(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.mq5").write_text("hi", encoding="utf-8")
    dest = tmp_path / "snaps"
    out = snapshot_sources([str(src / "a.mq5")], dest, label="run1")
    assert out["file_count"] == 1
    assert (Path(out["snapshot_dir"]) / "manifest.json").exists()
    snaps = list_snapshots(dest)
    assert len(snaps) == 1
    assert snaps[0]["label"] == "run1"


def test_top_passes():
    passes = [
        {"profit": 100, "drawdown": 50},
        {"profit": 300, "drawdown": 80},
        {"profit": 200, "drawdown": 40},
    ]
    out = top_passes(passes, criterion="profit", n=2)
    assert [p["profit"] for p in out] == [300, 200]
    out2 = top_passes(passes, criterion="drawdown", n=2, descending=False)
    assert [p["drawdown"] for p in out2] == [40, 50]
