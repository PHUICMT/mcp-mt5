from pathlib import Path

from mcp_mt5.lint import lint_basic, check_deprecated, validate_tester_ini


def test_lint_flags_missing_handlers(tmp_path: Path):
    src = tmp_path / "no_handlers.mq5"
    src.write_text("input int Inp = 1;\n", encoding="utf-8")
    out = lint_basic(src)
    rules = {f["rule"] for f in out["findings"]}
    assert "missing_entry_point" in rules
    assert "missing_oninit" in rules
    assert "missing_ondeinit" in rules


def test_lint_flags_unused_input(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text("""
input int InpUsed = 1;
input int InpUnused = 2;
int OnInit() { return InpUsed; }
void OnDeinit(const int reason) {}
void OnTick() {}
""", encoding="utf-8")
    out = lint_basic(src)
    unused = [f for f in out["findings"] if f.get("rule") == "unused_input"]
    assert len(unused) == 1
    assert unused[0]["name"] == "InpUnused"


def test_check_deprecated_flags_mt4_calls(tmp_path: Path):
    src = tmp_path / "old.mq5"
    src.write_text(
        "void OnTick() { OrderSend(_Symbol, OP_BUY, 0.1, Ask, 3, 0, 0); }\n",
        encoding="utf-8",
    )
    out = check_deprecated(src)
    funcs = {f["func"] for f in out}
    assert "OrderSend" in funcs
    assert "Ask" in funcs


def test_validate_tester_ini_required_keys(tmp_path: Path):
    cfg = tmp_path / "tester.ini"
    cfg.write_text("[Tester]\nSymbol=EURUSD\n", encoding="utf-8")
    out = validate_tester_ini(cfg)
    keys = {(i["section"], i["key"]) for i in out["issues"] if i.get("severity") != "info"}
    assert ("Tester", "Expert") in keys
    assert ("Tester", "Period") in keys


def test_validate_tester_ini_with_source(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text("input int InpKnown = 1;\n", encoding="utf-8")
    cfg = tmp_path / "tester.ini"
    cfg.write_text("""\
[Tester]
Expert=EA
Symbol=EURUSD
Period=15
FromDate=2024.01.01
ToDate=2024.12.31
Deposit=10000

[TesterInputs]
InpKnown=1||1||0||1||N
InpUnknown=0||0||0||0||N
""", encoding="utf-8")
    out = validate_tester_ini(cfg, source=src)
    msgs = [i["message"] for i in out["issues"]]
    assert any("InpUnknown" in m for m in msgs)
