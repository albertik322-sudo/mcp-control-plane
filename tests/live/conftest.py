from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.getenv("MCP_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="set MCP_LIVE_TESTS=1 and subsystem flag to run live MCP tests")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def require_live_flag(name: str) -> None:
    if os.getenv("MCP_LIVE_TESTS") != "1":
        pytest.skip("set MCP_LIVE_TESTS=1")
    if os.getenv(name) != "1":
        pytest.skip(f"set {name}=1")


@pytest.fixture(autouse=True)
def live_timeouts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REMOTE_CONNECT_TIMEOUT", os.getenv("REMOTE_CONNECT_TIMEOUT", "5"))
    monkeypatch.setenv("MIKROTIK_TIMEOUT", os.getenv("MIKROTIK_TIMEOUT", "4"))
    monkeypatch.setenv("HOMEASSISTANT_TIMEOUT", os.getenv("HOMEASSISTANT_TIMEOUT", "5"))
