from __future__ import annotations

import pytest

from tests.live.conftest import require_live_flag


pytestmark = [pytest.mark.live, pytest.mark.live_server]


def test_server_read_only_smoke() -> None:
    require_live_flag("MCP_LIVE_SERVER")
    import server

    overview = server.system_overview()
    services = server.service_status(["apache2"])

    assert overview["exit_code"] == 0
    assert services["exit_code"] == 0
