import os
from pathlib import Path

from mcp_mt5.paths import detect_layout, find_terminal_for_install, MT5Layout


def test_detect_layout_explicit_data(tmp_path: Path, monkeypatch):
    install = tmp_path / "MT5"
    install.mkdir()
    data = tmp_path / "data" / "ABCDEF"
    (data / "MQL5" / "Experts").mkdir(parents=True)
    (install / "terminal64.exe").write_bytes(b"")
    (install / "MetaEditor64.exe").write_bytes(b"")

    L = detect_layout(install=str(install), data=str(data))
    assert L.install == install
    assert L.data == data
    assert L.terminal_hash == "ABCDEF"
    assert L.metaeditor.exists()
    assert L.terminal.exists()
    assert L.experts_dir.exists()
    assert L.issues() == []


def test_detect_layout_mt4_edition(tmp_path: Path):
    install = tmp_path / "MT4"
    install.mkdir()
    (install / "terminal.exe").write_bytes(b"")
    (install / "metaeditor.exe").write_bytes(b"")
    data = tmp_path / "mt4data"
    (data / "MQL4" / "Experts").mkdir(parents=True)

    L = detect_layout(install=str(install), data=str(data), edition="mt4")
    assert L.edition == "mt4"
    assert L.mql_root.name == "MQL4"
    assert L.metaeditor.name == "metaeditor.exe"
    assert L.terminal.name == "terminal.exe"


def test_find_terminal_for_install(tmp_path: Path, monkeypatch):
    appdata = tmp_path / "AppData"
    base = appdata / "MetaQuotes" / "Terminal"
    hash_dir = base / ("A" * 32)
    hash_dir.mkdir(parents=True)
    install_path = r"C:\Program Files\Test MT5"
    (hash_dir / "origin.txt").write_text(install_path, encoding="utf-8")

    monkeypatch.setenv("APPDATA", str(appdata))
    found = find_terminal_for_install(Path(install_path))
    assert found is not None
    h, data = found
    assert h == "A" * 32
    assert data == hash_dir


def test_layout_issues_when_missing(tmp_path: Path):
    L = detect_layout(install=str(tmp_path / "missing"), data=str(tmp_path / "missing_data"))
    issues = L.issues()
    assert len(issues) > 0
    assert any("MetaEditor" in i for i in issues)
