from mcp_mt5.parsers import parse_compile_log, parse_tester_report, iter_journal_lines


SAMPLE_COMPILE_LOG = """
d:\\WORK\\RapidFire\\RapidFireEA.mq5 : information: compiling d:\\WORK\\RapidFire\\RapidFireEA.mq5
d:\\WORK\\RapidFire\\Include\\RapidFire\\Trail.mqh(7,20) : error 116: declaration without type
d:\\WORK\\RapidFire\\RapidFireEA.mq5(46,16) : warning 154: 'g_trade' - semicolon expected
Result: 1 errors, 1 warnings, 250 ms elapsed
"""


def test_parse_compile_log_extracts_diagnostics():
    out = parse_compile_log(SAMPLE_COMPILE_LOG)
    assert len(out["errors"]) == 1
    assert len(out["warnings"]) == 1
    e = out["errors"][0]
    assert e["line"] == 7 and e["col"] == 20 and e["code"] == 116
    assert "Trail.mqh" in e["file"]
    assert out["result_errors"] == 1
    assert out["result_warnings"] == 1
    assert out["ok"] is False


def test_parse_compile_log_clean_build():
    out = parse_compile_log("Result: 0 errors, 0 warnings, 100 ms elapsed")
    assert out["ok"] is True
    assert out["result_errors"] == 0


SAMPLE_REPORT = """
<html><body>
<table>
<tr><td>Expert:</td><td>RapidFireEA</td></tr>
<tr><td>Symbol:</td><td>XAUUSD</td></tr>
<tr><td>Total Net Profit:</td><td>1234.56</td></tr>
<tr><td>Profit Factor:</td><td>1.45</td></tr>
<tr><td>Total Trades:</td><td>87</td></tr>
</table>
<table>
<tr><th>Time</th><th>Type</th><th>Order</th><th>Size</th><th>Price</th><th>S/L</th><th>T/P</th><th>Profit</th><th>Balance</th></tr>
<tr><td>2024.01.01 00:00:00</td><td>buy</td><td>1</td><td>0.10</td><td>2050.00</td><td>2040</td><td>2070</td><td>200.00</td><td>10200</td></tr>
<tr><td>2024.01.02 00:00:00</td><td>sell</td><td>2</td><td>0.10</td><td>2070.00</td><td>2080</td><td>2050</td><td>-50.00</td><td>10150</td></tr>
</table>
</body></html>
"""


def test_parse_tester_report():
    out = parse_tester_report(SAMPLE_REPORT)
    s = out["summary"]
    assert s.get("expert") == "RapidFireEA"
    assert s.get("symbol") == "XAUUSD"
    assert s.get("net_profit") == "1234.56"
    assert s.get("profit_factor") == "1.45"
    assert s.get("total_trades") == "87"
    assert out["trade_rows_detected"] >= 2


SAMPLE_JOURNAL = """2024.01.01 12:00:00.123\tExpert RapidFireEA (XAUUSD,M5) loaded successfully
2024.01.01 12:00:01.456\tNetwork\t'1234567': connection lost
"""


def test_iter_journal_lines():
    records = list(iter_journal_lines(SAMPLE_JOURNAL))
    assert len(records) == 2
    assert records[0]["ts"].startswith("2024.01.01")
    assert "Expert" in records[0]["source"] or "RapidFireEA" in records[0]["message"]
    assert records[1]["source"] == "Network"
