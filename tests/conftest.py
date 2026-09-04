"""Shared fixtures: keep the module-level layout cache from leaking between tests."""
from __future__ import annotations

import pytest

from mcp_mt5 import server


@pytest.fixture(autouse=True)
def _isolate_layout_cache():
    server.reset_layout_cache()
    server._spawned_pids.clear()
    yield
    server.reset_layout_cache()
    server._spawned_pids.clear()
