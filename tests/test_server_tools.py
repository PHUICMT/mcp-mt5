"""Server-level tool tests with subprocess + filesystem mocked via tmp_path."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_mt5 import server
from mcp_mt5.paths import MT5Layout


@pytest.fixture
def fake_layout(tmp_path: Path, monkeypatch):
    install = tmp_path / "MT5"
    install.mkdir()
    (install / "terminal64.exe").write_bytes(b"")
    (install / "MetaEditor64.exe").write_bytes(b"")

    data = tmp_path / "data"
    for sub in ("MQL5/Experts", "MQL5/Include", "MQL5/Files", "MQL5/Logs", "Tester/logs"):
        (data / sub).mkdir(parents=True)

    L = MT5Layout(install=install, data=data, terminal_hash="TESTHASH", edition="mt5")
    monkeypatch.setattr(server, "_layout_cache", L)
    return L


def test_env_info(fake_layout):
    info = server.env_info()
    assert info["edition"] == "mt5"
    assert info["terminal_hash"] == "TESTHASH"
    assert info["issues"] == []


def test_compile_missing_source(fake_layout):
    out = server.compile("/no/such/file.mq5")
    assert "source not found" in out["error"]


def test_compile_invokes_metaeditor(fake_layout, tmp_path):
    src = tmp_path / "test.mq5"
    src.write_text("// test")
    log = tmp_path / ".mt5tmp" / "test.compile.log"

    def fake_run(cmd, capture_output, text, timeout):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Result: 0 errors, 0 warnings, 50 ms elapsed\n", encoding="utf-8")

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    with patch("subprocess.run", side_effect=fake_run):
        out = server.compile(str(src))
    assert out["ok"] is True
    assert out["result_errors"] == 0
    assert out["returncode"] == 0


def test_run_backtest_missing_config(fake_layout):
    out = server.run_backtest("/no/cfg.ini")
    assert "config not found" in out["error"]


def test_deploy_ea_copies(fake_layout, tmp_path):
    src = tmp_path / "MyEA.ex5"
    src.write_bytes(b"BINARYDATA")
    out = server.deploy_ea(str(src))
    target = fake_layout.experts_dir / "MyEA.ex5"
    assert target.exists()
    assert target.read_bytes() == b"BINARYDATA"
    assert out["copied_to"] == str(target)


def test_install_include(fake_layout, tmp_path):
    src = tmp_path / "LiveLog.mqh"
    src.write_text("#define LIVELOG 1")
    out = server.install_include(str(src))
    assert (fake_layout.include_dir / "LiveLog.mqh").exists()
    assert "LiveLog.mqh" in out["copied_to"]


def test_list_experts(fake_layout):
    (fake_layout.experts_dir / "A.ex5").write_bytes(b"x")
    (fake_layout.experts_dir / "Sub").mkdir()
    (fake_layout.experts_dir / "Sub" / "B.ex5").write_bytes(b"x")
    out = server.list_experts()
    assert out["count"] == 2
    names = {f["name"] for f in out["files"]}
    assert names == {"A.ex5", "B.ex5"}


def test_tail_log_journal(fake_layout, monkeypatch):
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    log = fake_layout.logs_dir / f"{today}.log"
    log.write_text("\n".join(f"line{i}" for i in range(200)), encoding="utf-8")

    out = server.tail_log(mode="journal", lines=10)
    assert out["line_count"] == 10
    assert "line199" in out["content"]


def test_patch_tester_ini_updates_existing(tmp_path):
    cfg = tmp_path / "tester.ini"
    cfg.write_text("[Tester]\nSymbol=XAUUSD\nPeriod=M5\n", encoding="utf-8")
    out = server.patch_tester_ini(str(cfg), {"Tester.Symbol": "EURUSD", "Tester.Deposit": "50000"})
    assert "Tester.Symbol" in out["applied"]
    assert "Tester.Deposit" in out["applied"]
    text = cfg.read_text(encoding="utf-8")
    assert "Symbol=EURUSD" in text
    assert "Deposit=50000" in text


def test_patch_tester_ini_creates_section(tmp_path):
    cfg = tmp_path / "tester.ini"
    cfg.write_text("[Tester]\nSymbol=XAUUSD\n", encoding="utf-8")
    server.patch_tester_ini(str(cfg), {"TesterInputs.RiskPct": "1.5"})
    text = cfg.read_text(encoding="utf-8")
    assert "[TesterInputs]" in text
    assert "RiskPct=1.5" in text
