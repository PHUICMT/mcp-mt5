"""Wire-level tests: what an MCP client actually sees from this server (in-process, no subprocess)."""
from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session as client_session

from mcp_mt5 import server
from mcp_mt5.paths import MT5Layout

pytestmark = pytest.mark.anyio

EXPECTED_TOOL_COUNT = 39


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


@pytest.fixture
async def session():
    async with client_session(server.mcp) as s:
        yield s


async def test_server_advertises_instructions():
    """The `instructions` field is what the client shows the model at connect time."""
    from mcp.server.lowlevel.server import Server

    lowlevel: Server = server.mcp._mcp_server  # noqa: SLF001
    assert lowlevel.instructions and "env_info" in lowlevel.instructions
    assert "absolute Windows path" in lowlevel.instructions


async def test_tool_catalogue_is_agent_ready(session):
    tools = (await session.list_tools()).tools
    assert len(tools) == EXPECTED_TOOL_COUNT
    assert len({t.name for t in tools}) == len(tools)
    for t in tools:
        assert t.description and len(t.description) >= 25, f"{t.name}: thin description"
        Draft202012Validator.check_schema(t.inputSchema)
        assert t.inputSchema.get("type") == "object"
        assert t.outputSchema is not None, f"{t.name}: no outputSchema (bare `-> dict`?)"
        assert t.annotations is not None, f"{t.name}: no annotations"
        assert t.annotations.readOnlyHint is not None and t.annotations.destructiveHint is not None
        assert t.annotations.openWorldHint is False
        for prop, spec in t.inputSchema.get("properties", {}).items():
            assert spec.get("description"), f"{t.name}.{prop} has no description"


async def test_read_only_and_destructive_hints_are_consistent(session):
    tools = {t.name: t for t in (await session.list_tools()).tools}
    for name in ("env_info", "tail_log", "read_tester_report", "lint_basic", "compare_reports"):
        assert tools[name].annotations.readOnlyHint is True and tools[name].annotations.destructiveHint is False
    for name in ("kill_terminal", "deploy_ea", "patch_tester_ini", "format_mql", "rename_symbol"):
        assert tools[name].annotations.readOnlyHint is False and tools[name].annotations.destructiveHint is True


async def test_call_tool_returns_structured_content(session, fake_layout):
    r = await session.call_tool("env_info", {})
    assert r.isError is False
    assert isinstance(r.content[0], types.TextContent)
    assert r.structuredContent is not None
    assert r.structuredContent["terminal_hash"] == "TESTHASH"


async def test_tool_failure_is_flagged_as_error(session, fake_layout):
    r = await session.call_tool("compile", {"source": "C:/nope/missing.mq5"})
    assert r.isError is True
    assert "source not found" in r.content[0].text
    assert r.structuredContent is None


async def test_module_error_is_flagged_as_error(session):
    r = await session.call_tool("lint_basic", {"source": "C:/nope/missing.mq5"})
    assert r.isError is True and "not found" in r.content[0].text


async def test_unknown_tool_and_missing_argument(session):
    r = await session.call_tool("does_not_exist", {})
    assert r.isError is True
    r = await session.call_tool("compile", {})
    assert r.isError is True


async def test_resources_listed_and_readable(session, fake_layout):
    uris = {str(r.uri) for r in (await session.list_resources()).resources}
    assert {"mt5://livelog", "mt5://journal", "mt5://tester-log"} <= uris
    (fake_layout.files_dir / "LiveLog.txt").write_text("hello\n", encoding="utf-8")
    read = await session.read_resource("mt5://livelog")
    assert "hello" in read.contents[0].text


async def test_run_backtest_sends_progress_over_the_wire(session, fake_layout, tmp_path: Path, monkeypatch):
    import subprocess

    cfg = tmp_path / "t.ini"
    cfg.write_text("[Tester]\nExpert=X\n", encoding="utf-8")

    class FakeProc:
        pid = 1

        def __init__(self, cmd, **kw):
            pass

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    r = await session.call_tool("run_backtest", {"config": str(cfg), "timeout_sec": 30}, progress_callback=on_progress)
    assert r.isError is False
    assert r.structuredContent["status"] == "completed"
    assert seen and seen[0][2].startswith("terminal launched")
    assert [s[0] for s in seen] == sorted(s[0] for s in seen)  # progress must not decrease


async def test_report_is_exposed_as_resource_not_inline_html(session, fake_layout):
    html = "<html><body><table><tr><td>Total Net Profit:</td><td>1 234.50</td></tr></table>" + "<p>" + "x" * 20000 + "</p></body></html>"
    (fake_layout.data / "tester_report.htm").write_text(html, encoding="utf-8")
    r = await session.call_tool("read_tester_report", {})
    assert r.isError is False
    sc = r.structuredContent
    assert sc["summary"]["net_profit"] == "1 234.50"
    assert "raw_truncated" not in sc and "trades" not in sc          # concise by default
    assert sc["report_uri"].startswith("mt5://report/")
    assert len(r.content[0].text) < 2000                              # the 20 KB HTML stayed out of the tool result
    templates = {str(t.uriTemplate) for t in (await session.list_resource_templates()).resourceTemplates}
    assert "mt5://report/{report_id}" in templates
    read = await session.read_resource(sc["report_uri"])
    assert "x" * 20000 in read.contents[0].text
    latest = await session.read_resource("mt5://report/latest")
    assert "1 234.50" in latest.contents[0].text
    detailed = await session.call_tool("read_tester_report", {"response_format": "detailed", "raw_truncate": 100})
    assert "trades" in detailed.structuredContent and len(detailed.structuredContent["raw_truncated"]) == 100
