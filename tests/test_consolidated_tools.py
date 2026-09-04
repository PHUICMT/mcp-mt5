"""Tests for the 0.5.0 consolidated tools (analyze_mql, inspect_source, compare_reports guards, deploy, compile syntax_only)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_mt5 import server
from mcp_mt5.paths import MT5Layout

EA = """\
#include "Helper.mqh"
input double InpRisk = 1.0;   // risk %
input int    InpUnused = 5;
//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit() { return INIT_SUCCEEDED; }
void OnDeinit(const int reason) {}
void OnTick()
{
   double lots = InpRisk * 0.01;
   double a = AccountBalance();
   OrderSend(_Symbol, OP_BUY, lots, Ask, 3, 0, 0, "", 12345, 0, clrNONE);
}
"""


@pytest.fixture
def fake_layout(tmp_path: Path, monkeypatch):
    install = tmp_path / "MT5"
    install.mkdir()
    (install / "terminal64.exe").write_bytes(b"")
    (install / "MetaEditor64.exe").write_bytes(b"")
    data = tmp_path / "data"
    for sub in ("MQL5/Experts", "MQL5/Include", "MQL5/Files", "MQL5/Logs", "Tester/logs"):
        (data / sub).mkdir(parents=True)
    L = MT5Layout(install=install, data=data, terminal_hash="H", edition="mt5")
    monkeypatch.setattr(server, "_layout_cache", L)
    return L


@pytest.fixture
def project(tmp_path: Path):
    src = tmp_path / "proj" / "MyEA.mq5"
    src.parent.mkdir()
    src.write_text(EA, encoding="utf-8")
    (src.parent / "Other.mq5").write_text("int MagicNumber = 12345;\nint Magic2 = 12345;\n", encoding="utf-8")
    return src


def test_analyze_mql_runs_all_checks(project):
    out = server.analyze_mql(str(project))
    assert out["checks"] == ["lint", "deprecated", "metrics"]
    assert any(f["rule"] == "unused_input" and f["name"] == "InpUnused" for f in out["lint"]["findings"])
    assert {d["func"] for d in out["deprecated"]} >= {"AccountBalance", "OrderSend"}
    assert out["metrics"]["function_count"] >= 3
    assert out["issue_count"] == len(out["lint"]["findings"]) + len(out["deprecated"])
    only = server.analyze_mql(str(project), checks=["metrics"])
    assert "lint" not in only and "metrics" in only
    with pytest.raises(ToolError, match="needs `source`"):
        server.analyze_mql(root=str(project.parent), checks=["lint"])


def test_inspect_source_default_aspects_and_symbol(project, fake_layout):
    out = server.inspect_source(str(project))
    assert out["aspects"] == ["inputs", "includes", "docs"]
    assert [i["name"] for i in out["inputs"]] == ["InpRisk", "InpUnused"]
    assert out["includes"]["missing"] == ["Helper.mqh"]
    assert "Expert initialization" in out["docs"][0]["text"]
    sym = server.inspect_source(str(project), symbol="InpRisk")
    assert sym["symbol"]["match_count"] == 2
    magic = server.inspect_source(root=str(project.parent))
    assert magic["aspects"] == ["magic"] and "12345" in magic["magic"]["duplicates"]
    with pytest.raises(ToolError):
        server.inspect_source()


REPORT = "<table><tr><td>Total Net Profit:</td><td>{p}</td></tr><tr><td>Profit Factor:</td><td>{pf}</td></tr></table>"


def test_compare_reports_with_and_without_guards(tmp_path: Path):
    a = tmp_path / "a.htm"
    b = tmp_path / "b.htm"
    a.write_text(REPORT.format(p="1000", pf="1.5"), encoding="utf-8")
    b.write_text(REPORT.format(p="500", pf="1.1"), encoding="utf-8")
    plain = server.compare_reports(str(a), str(b))
    assert "violations" not in plain and any(d["key"] == "net_profit" and d["pct"] == -50 for d in plain["diffs"])
    guarded = server.compare_reports(str(a), str(b), guards={"net_profit": -10})
    assert guarded["ok"] is False and guarded["violations"][0]["key"] == "net_profit"
    with pytest.raises(ToolError, match="missing"):
        server.compare_reports(str(a), str(tmp_path / "nope.htm"))


def test_deploy_routes_by_extension(fake_layout, tmp_path: Path):
    ex = tmp_path / "X.ex5"
    ex.write_bytes(b"MZ")
    inc = tmp_path / "Lib.mqh"
    inc.write_text("#define X 1", encoding="utf-8")
    assert server.deploy(str(ex))["kind"] == "expert" and (fake_layout.experts_dir / "X.ex5").exists()
    assert server.deploy(str(inc))["kind"] == "include" and (fake_layout.include_dir / "Lib.mqh").exists()
    (tmp_path / "MyEA.mq5").write_text("// source, not a binary", encoding="utf-8")
    with pytest.raises(ToolError, match="unsupported extension"):
        server.deploy(str(tmp_path / "MyEA.mq5"))


def test_compile_syntax_only_skips_binary_check(fake_layout, tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text("// ea")
    seen = {}

    def fake_run(cmd, capture_output, text, timeout):
        seen["cmd"] = cmd
        log = next(a.split(":", 1)[1] for a in cmd if a.startswith("/log:"))
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("Result: 0 errors, 0 warnings, 5 ms elapsed\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        out = server.compile(str(src), syntax_only=True)
    assert "/s" in seen["cmd"] and seen["cmd"].index("/s") == 1
    assert out["ok"] is True and out["syntax_only"] is True and out["binary_fresh"] is None
    assert out["log_path"].endswith("ea.syntax.log")
