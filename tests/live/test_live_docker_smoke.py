from __future__ import annotations

import pytest

from tests.live.conftest import require_live_flag


pytestmark = [pytest.mark.live, pytest.mark.live_docker]


def test_docker_read_only_smoke() -> None:
    require_live_flag("MCP_LIVE_DOCKER")
    import docker_mcp as docker

    info = docker.docker_info()
    containers = docker.docker_ps(all_containers=True)
    unhealthy = docker.docker_unhealthy()

    assert info["exit_code"] == 0
    assert containers["exit_code"] == 0
    assert "rows" in containers
    assert unhealthy["exit_code"] == 0
