from pathlib import Path

from mcp_mt5.analysis import (
    extract_inputs,
    gen_tester_inputs,
    resolve_includes,
    find_symbol,
    code_metrics,
    extract_doc,
    find_magic_collision,
)


SAMPLE_EA = """\
//+------------------------------------------------------------------+
//| Sample EA                                                        |
//+------------------------------------------------------------------+
#include "Lib/Helper.mqh"

input string  InpSymbol = "XAUUSD";       // trading symbol
input ENUM_TIMEFRAMES InpTF = PERIOD_M5;  // base timeframe
input double  InpRiskPct = 0.5;           // risk per trade
input int     InpMagic   = 990011;
sinput bool   InpVerbose = true;

int OnInit() { return INIT_SUCCEEDED; }
void OnDeinit(const int reason) {}
void OnTick() {
   double lot = InpRiskPct;
   if (lot > 0) {
      // dummy
   }
}
"""


def test_extract_inputs(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text(SAMPLE_EA, encoding="utf-8")
    out = extract_inputs(src)
    names = {i["name"] for i in out}
    assert names == {"InpSymbol", "InpTF", "InpRiskPct", "InpMagic", "InpVerbose"}
    risk = next(i for i in out if i["name"] == "InpRiskPct")
    assert risk["type"] == "double"
    assert risk["default"] == "0.5"
    assert risk["comment"].startswith("risk per trade")


def test_gen_tester_inputs_translates_period(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text(SAMPLE_EA, encoding="utf-8")
    block = gen_tester_inputs(src)
    assert "[TesterInputs]" in block
    assert "InpTF=5||" in block         # PERIOD_M5 → 5
    assert "InpVerbose=true||" in block
    assert "InpSymbol=XAUUSD||" in block


def test_resolve_includes(tmp_path: Path):
    lib = tmp_path / "Lib"
    lib.mkdir()
    (lib / "Helper.mqh").write_text("// helper", encoding="utf-8")
    src = tmp_path / "ea.mq5"
    src.write_text(SAMPLE_EA, encoding="utf-8")

    tree = resolve_includes(src)
    assert tree["exists"] is True
    assert len(tree["resolved"]) == 1
    assert tree["resolved"][0]["file"].endswith("Helper.mqh")
    assert tree["missing"] == []


def test_resolve_includes_reports_missing(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text(SAMPLE_EA, encoding="utf-8")
    tree = resolve_includes(src)
    assert "Lib/Helper.mqh" in tree["missing"]


def test_find_symbol(tmp_path: Path):
    (tmp_path / "a.mq5").write_text("void f() { int x = 1; OnTick(); }", encoding="utf-8")
    (tmp_path / "b.mqh").write_text('// OnTick mention in comment\nvoid OnTick() {}', encoding="utf-8")
    matches = find_symbol("OnTick", tmp_path)
    files = {m["file"] for m in matches}
    assert any(f.endswith("a.mq5") for f in files)
    assert any(f.endswith("b.mqh") for f in files)
    # comment-only OnTick on the first line of b.mqh should be skipped
    b_matches = [m for m in matches if m["file"].endswith("b.mqh")]
    assert all(m["line"] != 1 for m in b_matches)


def test_code_metrics(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text(SAMPLE_EA, encoding="utf-8")
    m = code_metrics(src)
    assert m["function_count"] >= 3
    assert m["code_lines"] > 0
    assert m["max_nesting"] >= 2


def test_extract_doc(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text(SAMPLE_EA, encoding="utf-8")
    blocks = extract_doc(src)
    assert len(blocks) >= 1
    assert "Sample EA" in blocks[0]["text"]


def test_find_magic_collision(tmp_path: Path):
    (tmp_path / "a.mq5").write_text("input int InpMagic = 990011;", encoding="utf-8")
    (tmp_path / "b.mq5").write_text("input int OtherMagic = 990011;", encoding="utf-8")
    out = find_magic_collision(tmp_path)
    assert "990011" in out["duplicates"]
    assert len(out["duplicates"]["990011"]) == 2
