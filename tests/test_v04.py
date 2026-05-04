"""Tests for v0.4.0 — terminal selection, smoke harness, AST refactor."""
from pathlib import Path

from mcp_mt5 import server
from mcp_mt5.paths import MT5Layout, list_terminal_origins
from mcp_mt5.ast_refactor import extract_function
from mcp_mt5.smoke import scan_journal_for_errors, write_smoke_tester_ini


def _fake_layout(tmp_path: Path) -> MT5Layout:
    install = tmp_path / "MT5"
    install.mkdir()
    (install / "terminal64.exe").write_bytes(b"")
    (install / "MetaEditor64.exe").write_bytes(b"")
    data = tmp_path / "data"
    for sub in ("MQL5/Experts", "MQL5/Include", "MQL5/Files", "MQL5/Logs", "Tester/logs"):
        (data / sub).mkdir(parents=True)
    return MT5Layout(install=install, data=data, terminal_hash="HASH", edition="mt5")


def test_list_terminal_origins(tmp_path: Path, monkeypatch):
    appdata = tmp_path / "AppData"
    base = appdata / "MetaQuotes" / "Terminal"
    h1 = base / ("A" * 32)
    h1.mkdir(parents=True)
    (h1 / "origin.txt").write_text(r"C:\Program Files\Test1", encoding="utf-8")
    h2 = base / ("B" * 32)
    h2.mkdir(parents=True)
    (h2 / "origin.txt").write_text(r"C:\Program Files\Test2", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))

    out = list_terminal_origins()
    assert len(out) == 2
    origins = {t["origin"].strip() for t in out}
    assert any("Test1" in o for o in origins)
    assert any("Test2" in o for o in origins)


def test_select_terminal_by_hash(tmp_path: Path, monkeypatch):
    appdata = tmp_path / "AppData"
    base = appdata / "MetaQuotes" / "Terminal"
    h = base / ("C" * 32)
    (h / "MQL5" / "Experts").mkdir(parents=True)
    (h / "origin.txt").write_text(r"C:\Program Files\TestSelect", encoding="utf-8")
    install = tmp_path / "Install"
    install.mkdir()
    (install / "terminal64.exe").write_bytes(b"")
    (install / "MetaEditor64.exe").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(appdata))

    monkeypatch.setattr(server, "_layout_cache", None)
    out = server.select_terminal(hash="C" * 32, install=str(install))
    assert out["active_hash"] == "C" * 32
    assert "TestSelect" not in out["active_install"]  # install passed explicitly


def test_extract_function_inline_dry_run(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text("""\
int OnInit() { return INIT_SUCCEEDED; }

void OnTick()
{
   double ask = 1.0;
   double bid = 2.0;
   double spread = ask - bid;
   if (spread > 0)
      Print(spread);
}
""", encoding="utf-8")
    out = extract_function(src, line_start=5, line_end=7, new_name="ComputeSpread",
                           return_type="double", dry_run=True)
    assert out["mode"] == "inline"
    assert "ComputeSpread" in out["helper"]
    assert "ComputeSpread(" in out["call_site"]
    assert out["enclosing_function"] == "OnTick"


def test_extract_function_external_writes(tmp_path: Path):
    src = tmp_path / "ea.mq5"
    src.write_text("""\
void OnTick()
{
   int a = 1;
   int b = 2;
   int sum = a + b;
}
""", encoding="utf-8")
    helper = tmp_path / "Helper.mqh"
    extract_function(src, line_start=3, line_end=5, new_name="Compute",
                     return_type="int", target_file=helper, dry_run=False)
    assert helper.exists()
    assert "int Compute(" in helper.read_text(encoding="utf-8")
    new_body = src.read_text(encoding="utf-8")
    assert "Compute(" in new_body


def test_extract_function_invalid_range(tmp_path: Path):
    src = tmp_path / "f.mq5"
    src.write_text("// just a comment\n", encoding="utf-8")
    out = extract_function(src, 5, 6, "Foo")
    assert "error" in out


def test_smoke_journal_scan(tmp_path: Path):
    log = tmp_path / "tester.log"
    log.write_text("""\
2024.01.01 00:00:00.000\tCore\tEA loaded
2024.01.01 00:00:00.500\tNetwork\terror: connection refused
2024.01.01 00:00:01.000\tSystem\tNo errors during init
2024.01.01 00:00:02.000\tEA\taccess violation in OnTick
""", encoding="utf-8")
    out = scan_journal_for_errors(log)
    assert out["match_count"] == 2
    assert any("connection refused" in m["text"] for m in out["matches"])
    assert any("access violation" in m["text"] for m in out["matches"])


def test_write_smoke_tester_ini(tmp_path: Path):
    cfg = tmp_path / "smoke.ini"
    write_smoke_tester_ini("MyEA", cfg, symbol="EURUSD", period="M15", days=2)
    text = cfg.read_text(encoding="utf-8")
    assert "Expert=MyEA" in text
    assert "Symbol=EURUSD" in text
    assert "ShutdownTerminal=1" in text
    assert "Visual=0" in text


def test_resources_registered():
    """Verify FastMCP knows about the new resources."""
    # mcp instance keeps registered resources internally; ensure decorators ran without error
    assert callable(server.livelog_resource)
    assert callable(server.journal_resource)
    assert callable(server.tester_log_resource)
