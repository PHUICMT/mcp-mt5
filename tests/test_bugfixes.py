"""Regression tests for the 2026-09 bug sweep (see CHANGELOG 0.4.2)."""
from __future__ import annotations

import os
import struct
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_mt5 import formatting, optimization, server, smoke
from mcp_mt5.ast_refactor import extract_function
from mcp_mt5.parsers import (
    detect_encoding,
    parse_compile_log,
    parse_tester_journal_notes,
    parse_tester_report,
    read_text_auto,
    write_text_preserving,
)
from mcp_mt5.paths import MT5Layout


@pytest.fixture
def fake_layout(tmp_path: Path, monkeypatch, edition: str = "mt5"):
    install = tmp_path / "MT5"
    install.mkdir()
    (install / "terminal64.exe").write_bytes(b"")
    (install / "MetaEditor64.exe").write_bytes(b"")
    data = tmp_path / "data"
    for sub in ("MQL5/Experts", "MQL5/Include", "MQL5/Files", "MQL5/Logs", "Tester/logs", "Tester/cache"):
        (data / sub).mkdir(parents=True)
    L = MT5Layout(install=install, data=data, terminal_hash="TESTHASH", edition="mt5")
    monkeypatch.setattr(server, "_layout_cache", L)
    return L


# --- #10 encoding ------------------------------------------------------------------

def test_detect_encoding_utf16_without_bom():
    raw = "input int InpRisk = 1; // สวัสดี".encode("utf-16-le")
    assert detect_encoding(raw) == "utf-16-le"
    assert detect_encoding(b"\xff\xfe" + raw) == "utf-16-le"
    assert detect_encoding("plain ascii".encode()) == "utf-8"
    assert detect_encoding(b"\xef\xbb\xbfabc") == "utf-8-sig"


def test_read_text_auto_handles_bomless_utf16(tmp_path: Path):
    f = tmp_path / "ea.mq5"
    f.write_bytes("int OnInit() { return 0; }".encode("utf-16-le"))
    assert read_text_auto(f) == "int OnInit() { return 0; }"


def test_write_text_preserving_keeps_utf16(tmp_path: Path):
    f = tmp_path / "tester.ini"
    f.write_bytes(b"\xff\xfe" + "[Tester]\r\nSymbol=EURUSD\r\n".encode("utf-16-le"))
    enc = write_text_preserving(f, "[Tester]\r\nSymbol=XAUUSD\r\n")
    assert enc == "utf-16-le"
    raw = f.read_bytes()
    assert raw[:2] == b"\xff\xfe"
    assert raw[2:].decode("utf-16-le") == "[Tester]\r\nSymbol=XAUUSD\r\n"


def test_patch_tester_ini_preserves_utf16(tmp_path: Path):
    cfg = tmp_path / "tester.ini"
    cfg.write_bytes(b"\xff\xfe" + "[Tester]\nSymbol=XAUUSD\n".encode("utf-16-le"))
    out = server.patch_tester_ini(str(cfg), {"Tester.Symbol": "EURUSD"})
    assert out["encoding"] == "utf-16-le"
    assert "Symbol=EURUSD" in read_text_auto(cfg)
    assert cfg.read_bytes()[:2] == b"\xff\xfe"


# --- #4 compile success detection --------------------------------------------------

def test_compile_log_without_result_line_is_not_ok():
    out = parse_compile_log("MyEA.mq5 : information: compiling 'MyEA.mq5'\n")
    assert out["ok"] is False
    assert out["result_line_found"] is False
    assert parse_compile_log("Result: 0 errors, 0 warnings, 10 ms elapsed")["ok"] is True


def test_compile_requires_fresh_binary(fake_layout, tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text("// ea")
    log = tmp_path / ".mt5tmp" / "ea.compile.log"

    def fake_run_no_binary(cmd, capture_output, text, timeout):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Result: 0 errors, 0 warnings, 50 ms elapsed\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run_no_binary):
        out = server.compile(str(src))
    assert out["ok"] is False and out["binary_fresh"] is False

    def fake_run_with_binary(cmd, capture_output, text, timeout):
        fake_run_no_binary(cmd, capture_output, text, timeout)
        src.with_suffix(".ex5").write_bytes(b"MZ")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run_with_binary):
        out = server.compile(str(src))
    assert out["ok"] is True and out["binary_fresh"] is True


# --- #5 smoke "0 errors" substring bug ---------------------------------------------

def test_smoke_rejects_ten_errors(fake_layout, tmp_path: Path, monkeypatch):
    src = tmp_path / "ea.mq5"
    src.write_text("// ea")
    monkeypatch.setenv("MT5_WORK_DIR", str(tmp_path / "work"))

    def fake_run(cmd, **kw):
        log = next(a.split(":", 1)[1] for a in cmd if a.startswith("/log:"))
        Path(log).write_text("Result: 10 errors, 0 warnings, 50 ms elapsed\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        out = smoke.run_smoke(fake_layout, src)
    assert out["ok"] is False and out["stage"] == "compile"
    assert "10 compile errors" in out["error"]


# --- #7 / #8 optimization ----------------------------------------------------------

def _build_opt_file(path: Path, passes: list[tuple[int, float, int]]) -> None:
    """Synthesise a TesterOptCache file following the documented struct layout."""
    def ws(s: str, n: int) -> bytes:
        return s.encode("utf-16-le").ljust(n, b"\x00")[:n]

    # one optimised int input "InpPeriod" and one plain double input "InpLot"
    inputs = [
        struct.pack(optimization._INPUT_FMT, ws("InpPeriod", 128), 1, 7 + 75, 0, 0, 8, 0, struct.pack("<3q", 5, 1, 50)),
        struct.pack(optimization._INPUT_FMT, ws("InpLot", 128), 0, 13 + 75, 2, 8, 8, 0, struct.pack("<3d", 0.1, 0, 0.1)),
    ]
    opt_params_size, dwords_cnt = 8, 0
    record_size = optimization.SUMMARY_SIZE + opt_params_size + dwords_cnt * 4
    header = struct.pack(
        optimization._HEADER_FMT,
        3, ws("Copyright 2000-2026, MetaQuotes Ltd.", 128), ws("TesterOptCache", 32), *([0] * 66),
        optimization.HEADER_SIZE, record_size, ws("MyEA", 128), ws("Experts\\MyEA.ex5", 256), ws("Demo", 128),
        ws("EURUSD", 64), 15, 1704067200, 1735603200, 0, 0, 0, 0, 0, 0, 0, *([0] * 16),
        ws("", 160), ws("USD", 64), 10000, 0, 500, 2, 2, 0, *([0] * 5), b"\x00" * 16,
        8, len(inputs), opt_params_size, 1, dwords_cnt, 0, len(passes), len(passes),
    )
    body = b"".join(inputs) + b"\x00" * 8  # common (non-optimised) parameter buffer
    for pass_id, profit, period in passes:
        doubles = [10000.0, 0.0, profit] + [0.0] * 24
        ints = [10, 5] + [0] * 12
        body += struct.pack(optimization._SUMMARY_FMT, pass_id, *doubles, *ints) + struct.pack("<q", period)
    path.write_bytes(header + body)


def test_parse_opt_file_documented_layout(tmp_path: Path):
    f = tmp_path / "MyEA.EURUSD.M15.20240101.20241231.00.ABCDEF.opt"
    _build_opt_file(f, [(i, float(100 * i), 5 + i) for i in range(60)])
    out = optimization.parse_opt_file(f)
    assert out["format"] == "TesterOptCache"
    assert out["header"]["expert_name"] == "MyEA" and out["header"]["symbol"] == "EURUSD"
    assert out["optimized_inputs"] == ["InpPeriod"]
    assert out["pass_count"] == 60
    assert out["passes"][7]["profit"] == 700.0
    assert out["passes"][7]["inputs"] == {"InpPeriod": 12}
    assert out["passes"][7]["trades"] == 5


def test_parse_opt_file_refuses_to_guess(tmp_path: Path):
    f = tmp_path / "junk.opt"
    f.write_bytes(b"\x00" * 4096)
    out = optimization.parse_opt_file(f)
    assert "error" in out and "signature" in out["error"]


def test_top_passes_uses_all_passes_not_sample(fake_layout, tmp_path: Path):
    f = fake_layout.tester_dir / "cache" / "MyEA.EURUSD.M15.20240101.20241231.00.ABCDEF.opt"
    # best pass is #59, which the old 50-pass sample never saw
    _build_opt_file(f, [(i, float(100 * i), 5 + i) for i in range(60)])
    out = server.read_optimization(criterion="profit", top_n=1)
    assert out["pass_count"] == 60
    assert out["top"][0]["pass"] == 59
    assert "passes" not in out  # full list stays server-side; only the ranking is returned


def test_find_latest_opt_filters_by_name(tmp_path: Path):
    a = tmp_path / "A.EURUSD.M15.1.2.00.X.opt"
    b = tmp_path / "B.XAUUSD.H1.1.2.00.Y.opt"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    assert optimization.find_latest_opt(tmp_path, expert="A").endswith("A.EURUSD.M15.1.2.00.X.opt")
    assert optimization.find_latest_opt(tmp_path, symbol="XAUUSD").endswith("Y.opt")
    assert optimization.find_latest_opt(tmp_path, expert="Nope") is None


# --- #9 format_mql -----------------------------------------------------------------

def test_format_protects_mql_literals_and_defaults_to_dry_run(tmp_path: Path, monkeypatch):
    src = tmp_path / "ea.mq5"
    original = (
        '#property copyright "me"\n'
        'input group "Risk"\n'
        "datetime start = D'2024.01.01 00:00';\n"
        "color c = C'255,0,0';\n"
        "int  x=1;\n"
    )
    src.write_text(original, encoding="utf-8")
    monkeypatch.setattr(formatting, "has_clang_format", lambda: True)

    seen = {}

    def fake_clang(text, style):
        seen["input"] = text
        return 0, text.replace("int  x=1;", "int x = 1;"), ""

    monkeypatch.setattr(formatting, "_run_clang_format", fake_clang)
    out = formatting.format_mql(src)
    assert "D'2024.01.01 00:00'" not in seen["input"] and "input group" not in seen["input"]
    assert out["changed"] is True and out["written"] is False  # dry run by default
    assert src.read_text(encoding="utf-8") == original

    out = formatting.format_mql(src, write=True)
    text = src.read_text(encoding="utf-8")
    assert out["written"] is True
    assert "D'2024.01.01 00:00'" in text and "C'255,0,0'" in text
    assert 'input group "Risk"' in text and '#property copyright "me"' in text
    assert "int x = 1;" in text


# --- #3 report discovery -----------------------------------------------------------

REPORT = "<html><body><table><tr><td>Total Net Profit:</td><td>{p}</td></tr></table></body></html>"


def test_read_tester_report_finds_report_in_data_root(fake_layout):
    (fake_layout.tester_dir / "old.htm").write_text(REPORT.format(p="1"), encoding="utf-8")
    past = time.time() - 3600
    os.utime(fake_layout.tester_dir / "old.htm", (past, past))
    (fake_layout.data / "tester_report.htm").write_text(REPORT.format(p="42"), encoding="utf-8")
    out = server.read_tester_report()
    assert out["path"].endswith("tester_report.htm")
    assert out["summary"]["net_profit"] == "42"


def test_report_path_from_ini(fake_layout, tmp_path: Path):
    cfg = tmp_path / "t.ini"
    cfg.write_text("[Tester]\nExpert=X\nReport=reports\\run1\nOptimization=0\n", encoding="utf-8")
    p = server._report_path_from_ini(cfg, fake_layout)
    assert p == fake_layout.data / "reports" / "run1.htm"
    cfg.write_text("[Tester]\nReport=opt\nOptimization=2\n", encoding="utf-8")
    assert server._report_path_from_ini(cfg, fake_layout, portable=True) == fake_layout.install / "opt.xml"


# --- #11 / #12 / #15 run_backtest & kill_terminal -------------------------------------

def test_run_backtest_detaches_stdio_and_reports_journal(fake_layout, tmp_path: Path, monkeypatch):
    import anyio

    cfg = tmp_path / "t.ini"
    cfg.write_text("[Tester]\nExpert=X\nReport=rep\n", encoding="utf-8")
    calls = {}

    class FakeProc:
        pid = 4242

        def __init__(self, cmd, **kw):
            calls["cmd"], calls["kw"] = cmd, kw
            (fake_layout.tester_logs / "20260904.log").write_text(
                "2026.09.04 10:00:00.000\tTester\tstart time changed to 2024.03.01 00:00 to provide data at beginning\n",
                encoding="utf-8",
            )
            (fake_layout.data / "rep.htm").write_text(REPORT.format(p="7"), encoding="utf-8")

        def wait(self, timeout=None):
            time.sleep(0.05)
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    progress = []

    async def on_progress(p, total, msg):
        progress.append((p, total, msg))

    out = anyio.run(server.run_backtest_impl, str(cfg), True, 60, False, on_progress, 0.01)
    assert calls["kw"]["stdout"] is subprocess.DEVNULL and calls["kw"]["stderr"] is subprocess.DEVNULL
    assert out["status"] == "completed" and out["returncode"] == 0 and out["pid"] == 4242
    assert out["report_path"].endswith("rep.htm")
    assert out["journal_notes"]["start_time_changed"].startswith("2024.03.01")
    assert any("moved the start date" in w for w in out["warnings"])
    assert 4242 not in server._spawned_pids  # released after exit
    assert len(progress) >= 2 and progress[-1][2].startswith("terminal exited")
    assert server.get_backtest(out["run_id"])["status"] == "completed"
    assert any(r["run_id"] == out["run_id"] for r in server.get_backtest()["runs"])


def test_start_backtest_returns_immediately_and_can_be_cancelled(fake_layout, tmp_path: Path, monkeypatch):
    import threading

    cfg = tmp_path / "t.ini"
    cfg.write_text("[Tester]\nExpert=X\n", encoding="utf-8")
    killed = threading.Event()

    class SlowProc:
        pid = 777

        def __init__(self, cmd, **kw):
            self._done = threading.Event()

        def wait(self, timeout=None):
            if not self._done.wait(timeout):
                raise subprocess.TimeoutExpired("x", timeout)
            return -9

        def poll(self):
            return -9 if self._done.is_set() else None

        def kill(self):
            killed.set()
            self._done.set()

    monkeypatch.setattr(subprocess, "Popen", SlowProc)
    out = server.start_backtest(str(cfg), timeout_sec=30)
    assert out["status"] == "running" and 777 in server._spawned_pids
    out = server.cancel_backtest(out["run_id"])
    assert killed.wait(1.0)
    time.sleep(0.1)
    final = server.get_backtest(out["run_id"])
    assert final["status"] == "cancelled" and 777 not in server._spawned_pids


def test_kill_terminal_only_targets_spawned_pids(fake_layout):
    server._spawned_pids.add(999)
    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    with patch("subprocess.run", side_effect=fake_run):
        out = server.kill_terminal()
        assert out["scope"] == "spawned_by_server" and calls[0][:3] == ["taskkill", "/F", "/PID"]
        out = server.kill_terminal()
        assert out["killed"] == [] and "all_instances" in out["note"]
        out = server.kill_terminal(all_instances=True)
        assert out["scope"] == "all_instances" and "/IM" in calls[-1]


def test_journal_notes_parser():
    text = (
        "2026.09.04 10:00:00.000\tTester\thistory data begins from 2019.01.02 00:00\n"
        "2026.09.04 10:00:00.000\tTester\tXAUUSD: tested with error\n"
    )
    out = parse_tester_journal_notes(text)
    assert out["notes"]["history_begins"] == "2019.01.02 00:00"
    assert out["notes"]["tested_with_error"] is True
    assert len(out["warnings"]) == 1


# --- #13 list_experts default per edition -----------------------------------------------

def test_list_experts_default_pattern_follows_edition(tmp_path: Path, monkeypatch):
    install = tmp_path / "MT4"
    install.mkdir()
    (install / "terminal.exe").write_bytes(b"")
    data = tmp_path / "data4"
    (data / "MQL4" / "Experts").mkdir(parents=True)
    (data / "MQL4" / "Experts" / "A.ex4").write_bytes(b"x")
    L = MT5Layout(install=install, data=data, terminal_hash="H", edition="mt4")
    monkeypatch.setattr(server, "_layout_cache", L)
    assert server.list_experts()["count"] == 1


# --- audit B8: extract_function parameter scope ----------------------------------------

def test_extract_function_ignores_globals_and_other_functions(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text(
        "int gCounter = 0;\n"
        "void Other() { double stray = 1.0; }\n"
        "void OnTick()\n{\n   double ask = 1.0;\n   double bid = 2.0;\n   double spread = ask - bid;\n}\n",
        encoding="utf-8",
    )
    out = extract_function(src, line_start=7, line_end=7, new_name="Spread", return_type="double")
    assert set(out["params"]) == {"double ask", "double bid"}


# --- report parser exposes all trades ------------------------------------------------------

def test_parse_tester_report_returns_all_trades():
    rows = "".join(
        f"<tr><td>2024.01.{i:02d}</td><td>buy</td><td>{i}</td><td>0.1</td><td>1</td><td>1</td><td>1</td><td>{i}</td></tr>"
        for i in range(1, 13)
    )
    out = parse_tester_report(f"<table>{rows}</table>")
    assert out["trade_rows_detected"] == 12 and len(out["trades"]) == 12 and len(out["trades_sample"]) == 5


# --- found with real MT5/MT4 reports from public repos (2026-09-04) -------------------------

MT5_LIKE = """<table>
<tr><td>Expert:</td><td>Main</td></tr>
<tr><td>Symbol:</td><td>US500.test.raw</td></tr>
<tr><td>Initial Deposit:</td><td>10 000.00</td></tr>
<tr><td>Total Net Profit:</td><td>920.56</td><td>Balance Drawdown Absolute:</td><td>0.00</td><td>Equity Drawdown Absolute:</td><td>0.00</td></tr>
<tr><td>Gross Profit:</td><td>2 187.15</td><td>Balance Drawdown Maximal:</td><td>359.61 (3.34%)</td><td>Equity Drawdown Maximal:</td><td>457.61 (4.23%)</td></tr>
<tr><td>History Quality:</td><td>99%</td><td>Sharpe Ratio:</td><td>3.85</td></tr>
</table>
<table>
<tr><th>Time</th><th>Deal</th><th>Symbol</th><th>Type</th><th>Direction</th><th>Volume</th><th>Price</th><th>Order</th><th>Commission</th></tr>
<tr><td>2022.01.05 01:00:00</td><td>2</td><td>US500.test.raw</td><td>buy</td><td>in</td><td>2.08</td><td>4770.08</td><td>2</td><td>0.00</td></tr>
</table>"""

MT4_LIKE = """<table>
<tr><td>Symbol</td><td colspan=12>EURUSD (Euro vs US Dollar)</td></tr>
<tr><td>Bars in test</td><td>3018</td><td>Ticks modelled</td><td>1317040</td><td>Modelling quality</td><td>25.00%</td></tr>
<tr><td>Initial deposit</td><td>10000.00</td><td>Spread</td><td>Current (48)</td></tr>
<tr><td>Total net profit</td><td>1800.93</td><td>Gross profit</td><td>4810.08</td><td>Gross loss</td><td>-3009.15</td></tr>
<tr><td>Profit factor</td><td>1.60</td><td>Expected payoff</td><td>225.12</td></tr>
<tr><td>Maximal drawdown</td><td>5286.59 (44.14%)</td><td>Relative drawdown</td><td>44.14% (5286.59)</td></tr>
<tr><td>Total trades</td><td>8</td><td>Short positions (won %)</td><td>3 (33.33%)</td><td>Long positions (won %)</td><td>5 (60.00%)</td></tr>
</table>"""


def test_mt5_report_header_row_does_not_overwrite_symbol():
    s = parse_tester_report(MT5_LIKE)["summary"]
    assert s["symbol"] == "US500.test.raw"          # was "Type" (deals-table header) before the fix
    assert s["balance_drawdown_max"] == "359.61 (3.34%)" and s["equity_drawdown_max"] == "457.61 (4.23%)"
    assert s["history_quality"] == "99%" and s["sharpe_ratio"] == "3.85"


def test_mt4_report_sentence_case_labels():
    s = parse_tester_report(MT4_LIKE)["summary"]
    assert s["net_profit"] == "1800.93" and s["profit_factor"] == "1.60"
    assert s["max_drawdown"] == "5286.59 (44.14%)" and s["total_trades"] == "8"
    assert s["history_quality"] == "25.00%" and s["long_trades_won_pct"] == "5 (60.00%)"


def test_compare_reports_parses_space_grouped_numbers(tmp_path: Path):
    from mcp_mt5.reports import _to_float, compare_reports
    assert _to_float("10 000.00") == 10000.0
    assert _to_float("2 187.15") == 2187.15
    assert _to_float("359.61 (3.34%)") == 359.61
    assert _to_float("n/a") is None
    a = tmp_path / "a.htm"
    a.write_text(MT5_LIKE, encoding="utf-8")
    b = tmp_path / "b.htm"
    b.write_text(MT5_LIKE.replace("2 187.15", "1 795.23"), encoding="utf-8")
    d = next(x for x in compare_reports(a, b)["diffs"] if x["key"] == "gross_profit")
    assert d["delta"] == -391.92


# --- WSL path translation ----------------------------------------------------------------

def test_win_path_translates_only_wsl_mount_paths(monkeypatch):
    from mcp_mt5 import winpath

    monkeypatch.setattr(winpath.sys, "platform", "linux")
    monkeypatch.setattr(winpath, "_wslpath_w", lambda s: "C:\\\\" + s[len("/mnt/c/"):].replace("/", "\\\\"))
    assert winpath.win_path("/mnt/c/Users/me/EA.mq5") == "C:\\\\Users\\\\me\\\\EA.mq5"
    assert winpath.win_path("/tmp/x/EA.mq5") == "/tmp/x/EA.mq5"          # not a Windows mount: untouched
    monkeypatch.setattr(winpath.sys, "platform", "win32")
    assert winpath.win_path("/mnt/c/whatever") == "/mnt/c/whatever"        # native Windows: no-op


# --- found against a real MT5 build 6162 data folder (2026-09-04) ----------------------------

def test_detect_layout_honours_mt5_data_env(tmp_path: Path, monkeypatch):
    from mcp_mt5.paths import detect_layout
    data = tmp_path / "Terminal" / "ABCDEF0123456789ABCDEF0123456789"
    (data / "MQL5").mkdir(parents=True)
    monkeypatch.setenv("MT5_INSTALL", str(tmp_path / "MT5"))
    monkeypatch.setenv("MT5_DATA", str(data))
    monkeypatch.delenv("MT5_TERMINAL_HASH", raising=False)
    L = detect_layout()
    assert L.data == data and L.terminal_hash == "ABCDEF0123456789ABCDEF0123456789"


def test_journal_parser_reads_current_tab_separated_format():
    from mcp_mt5.parsers import iter_journal_lines
    text = ("PK\t0\t13:48:37.803\tTerminal\tMetaTrader 5 x64 build 6162 started for MetaQuotes Ltd.\n"
            "RS\t0\t13:49:29.187\tMQL5.community\tactivated for 'User', balance: 0.47\n")
    recs = list(iter_journal_lines(text, date="20260904"))
    assert len(recs) == 2
    assert recs[0] == {"ts": "2026.09.04 13:48:37.803", "source": "Terminal",
                       "message": "MetaTrader 5 x64 build 6162 started for MetaQuotes Ltd.", "code": "PK"}
    # the older dated format still parses
    assert list(iter_journal_lines("2024.01.01 12:00:00.123\tNetwork\tconnection lost"))[0]["source"] == "Network"


def test_lint_accepts_void_parameter_lists_and_counts_methods(tmp_path: Path):
    from mcp_mt5.analysis import code_metrics
    from mcp_mt5.lint import lint_basic
    src = tmp_path / "wizard.mq5"
    src.write_text(
        "class CSampleExpert { public: bool Init(void); void Process(const int x) const; };\n"
        "bool CSampleExpert::Init(void)\n  {\n   return(true);\n  }\n"
        "void CSampleExpert::Process(const int x) const\n  {\n  }\n"
        "int OnInit(void)\n  {\n   return(INIT_SUCCEEDED);\n  }\n"
        "void OnDeinit(const int reason)\n  {\n  }\n"
        "void OnTick(void)\n  {\n  }\n",
        encoding="utf-8",
    )
    rules = {f["rule"] for f in lint_basic(src)["findings"]}
    assert not ({"missing_entry_point", "missing_oninit", "missing_ondeinit"} & rules)
    assert code_metrics(src)["function_count"] == 5


def test_tail_log_terminal_mode_parses_dated_records(tmp_path: Path, monkeypatch):
    install = tmp_path / "MT5"
    install.mkdir()
    (install / "terminal64.exe").write_bytes(b"")
    (install / "MetaEditor64.exe").write_bytes(b"")
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "MQL5" / "Experts").mkdir(parents=True)
    (data / "logs" / "20260904.log").write_bytes(
        b"\xff\xfe" + "PK\t0\t13:48:37.803\tTerminal\tstarted\n".encode("utf-16-le"))
    monkeypatch.setattr(server, "_layout_cache", MT5Layout(install=install, data=data, terminal_hash="H", edition="mt5"))
    out = server.tail_log(mode="terminal", date="20260904", structured=True)
    assert out["records"][0]["ts"] == "2026.09.04 13:48:37.803" and out["records"][0]["source"] == "Terminal"


def test_strip_comments_keeps_code_after_url_in_string():
    from mcp_mt5.analysis import _strip_comments_strings
    src = ('#property link      "https://www.mql5.com"\n'
           '#property version   "5.50"\n'
           'input double InpLots = 0.1; // Lots\n'
           "/* block\n   comment */ string s = \"a // not a comment\"; char c = '\\''; datetime d = D'2024.01.01';\n"
           "bool ok = InpLots > 0;   // use\n")
    cleaned = _strip_comments_strings(src)
    assert cleaned.count("\n") == src.count("\n")                 # line numbers preserved
    assert "InpLots" in cleaned and cleaned.count("InpLots") == 2 # declaration + use survive
    assert "mql5.com" not in cleaned and "not a comment" not in cleaned and "block" not in cleaned
    assert "Lots" not in cleaned.replace("InpLots", "")            # comments gone
    assert "bool ok" in cleaned


def test_check_deprecated_ignores_methods_and_mql5_bars_function(tmp_path: Path):
    from mcp_mt5.lint import check_deprecated
    src = tmp_path / "ea.mq5"
    src.write_text(
        "double a = m_symbol.Ask();\n"        # method on a CSymbolInfo object: fine
        "int n = Bars(_Symbol, _Period);\n"   # MQL5 function: fine
        "int k = rates.Bars;\n"               # member: fine
        "double b = Bid;\n"                   # MT4 predefined variable: flag
        "int m = Bars;\n"                     # MT4 predefined variable: flag
        "OrderSend(Symbol(), OP_BUY, 0.1, Ask, 3, 0, 0);\n",  # MT4 call + bare Ask: flag both
        encoding="utf-8",
    )
    found = sorted((f["line"], f["func"]) for f in check_deprecated(src))
    assert found == [(4, "Bid"), (5, "Bars"), (6, "Ask"), (6, "OrderSend")]


def test_wsl_autodetect_scans_mnt_and_matches_windows_origin(tmp_path: Path, monkeypatch):
    from mcp_mt5 import paths
    mnt = tmp_path / "mnt"
    install = mnt / "c" / "Program Files" / "MetaTrader 5"
    install.mkdir(parents=True)
    data = mnt / "c" / "Users" / "me" / "AppData" / "Roaming" / "MetaQuotes" / "Terminal" / ("A" * 32)
    data.mkdir(parents=True)
    (data / "origin.txt").write_bytes(b"\xff\xfe" + r"C:\Program Files\MetaTrader 5".encode("utf-16-le"))
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("MT5_INSTALL", raising=False)
    monkeypatch.delenv("MT5_DATA", raising=False)
    monkeypatch.delenv("MT5_TERMINAL_HASH", raising=False)
    monkeypatch.setattr(paths, "win_path", lambda p: "C:\\" + str(p).split("/mnt/c/", 1)[1].replace("/", "\\"))
    monkeypatch.setattr(paths, "_MNT_ROOT", mnt)
    L = paths.detect_layout()
    assert L.install == install and L.data == data and L.terminal_hash == "A" * 32
    assert paths.list_terminal_origins()[0]["hash"] == "A" * 32


# --- found by running the real pipeline through WSL interop against MT5 build 6162 -------------------

def test_syntax_check_result_line_lower_case_without_colon():
    out = parse_compile_log(" : information: result 0 errors, 0 warnings, 84 ms elapsed, cpu='X64 Regular'")
    assert out["ok"] is True and out["result_line_found"] is True


def test_journal_notes_detect_history_sync_failure_and_success():
    failed = ("OO\t2\t14:18:57.489\tCore 3\tpass 2 tested with error \"cannot synchronize history (EURUSD)\" in 0:00:00.001\n"
              "XX\t0\t14:18:33.799\tTester\tlast test passed with result \"some error after pass finished\" in 0:00:00.000\n"
              "YY\t0\t14:18:30.952\tAutoTesting\tEURUSD: preliminary downloading of M1 history completed in 0:00:00.344\n")
    out = parse_tester_journal_notes(failed)
    assert out["notes"]["history_sync_failed"].startswith("EURUSD")
    assert out["notes"]["history_download"] == "M1 completed"
    assert any("did not run" in w and "Run the backtest again" in w for w in out["warnings"])
    ok = ("A\t0\t14:19:28.723\tCore 1\tEURUSD: history synchronized from 2025.01.01 to 2026.09.02\n"
          "B\t0\t14:19:28.723\tCore 1\tfinal balance 10187.54 USD\n"
          "C\t0\t14:19:28.723\tCore 1\tEURUSD,M15: 783 ticks, 204 bars generated. Test passed in 0:00:00.243.\n")
    out = parse_tester_journal_notes(ok)
    assert out["warnings"] == [] and out["notes"]["test_passed"] == "0:00:00.243." and out["notes"]["final_balance"].startswith("10187.54")


def test_real_opt_layout_offsets(tmp_path: Path):
    """Descriptors start right after the fixed header; header_size covers descriptors + common buffer."""
    f = tmp_path / "x.opt"
    _build_opt_file(f, [(i, float(i), 5) for i in range(3)])
    raw = bytearray(f.read_bytes())
    # rewrite header_size the way MT5 does (struct + descriptors + parameters_size) and confirm parsing still works
    hs = optimization.HEADER_SIZE + 2 * optimization.INPUT_SIZE + 8
    struct.pack_into("<I", raw, struct.calcsize("<I128s32s66i"), hs)  # header_size slot follows version/copyright/name/reserve
    f.write_bytes(raw)
    out = optimization.parse_opt_file(f)
    assert out["header"]["header_size"] == hs and out["pass_count"] == 3 and out["passes"][2]["inputs"] == {"InpPeriod": 5}


def test_run_registry_reads_only_appended_log_text(fake_layout, tmp_path: Path, monkeypatch):
    import anyio
    log = fake_layout.tester_logs / "20260904.log"
    log.write_bytes(b"\xff\xfe" + "AA\t0\t10:00:00.000\tCore 1\tpass 1 tested with error \"cannot synchronize history (EURUSD)\"\n".encode("utf-16-le"))
    cfg = tmp_path / "t.ini"
    cfg.write_text("[Tester]\nExpert=X\nReport=rep\n", encoding="utf-8")

    class FakeProc:
        pid = 5

        def __init__(self, cmd, **kw):
            with open(log, "ab") as fh:  # this run appends a clean pass
                fh.write("BB\t0\t10:05:00.000\tCore 1\tfinal balance 10100.00 USD\nCC\t0\t10:05:00.100\tCore 1\tTest passed in 0:00:00.200.\n".encode("utf-16-le"))

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    out = anyio.run(server.run_backtest_impl, str(cfg), True, 60, False, None, 0.01)
    assert out["warnings"] == []                     # the earlier failure in the same daily file is ignored
    assert out["journal_notes"]["test_passed"] == "0:00:00.200."
    assert "retried_after_history_sync" not in out
