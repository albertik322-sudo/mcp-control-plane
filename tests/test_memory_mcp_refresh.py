from __future__ import annotations

import threading
import time

import context_collector
import memory_mcp


def test_refresh_returns_busy_instead_of_stacking_work() -> None:
    assert memory_mcp._REFRESH_LOCK.acquire(blocking=False)
    try:
        with memory_mcp._REFRESH_STATE_LOCK:
            memory_mcp._ACTIVE_REFRESH = {"sources": ["server"], "started_at": 123.0}
        result = memory_mcp.refresh_context(["project"], publish_observability=False)
    finally:
        with memory_mcp._REFRESH_STATE_LOCK:
            memory_mcp._ACTIVE_REFRESH = None
        memory_mcp._REFRESH_LOCK.release()

    assert result["mode"] == "busy"
    assert result["exit_code"] == 2
    assert result["active_refresh"]["sources"] == ["server"]


def test_refresh_runs_independent_sources_in_parallel(monkeypatch) -> None:
    state = {"active": 0, "max_active": 0}
    state_lock = threading.Lock()

    def fake_collect(sources, publish=False):
        source = sources[0]
        with state_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.04)
        with state_lock:
            state["active"] -= 1
        return {source: {"ok": True, "duration_seconds": 0.04}}

    monkeypatch.setattr(context_collector, "collect_selected", fake_collect)
    result = memory_mcp.refresh_context(
        ["project", "tools", "windows", "server"],
        publish_observability=False,
        skip_fresh=False,
        max_workers=4,
    )

    assert result["mode"] == "parallel-in-process"
    assert result["exit_code"] == 0
    assert state["max_active"] >= 2
    assert result["refreshed_sources"] == ["project", "tools", "windows", "server"]


def test_full_refresh_skips_sources_that_are_already_fresh(monkeypatch) -> None:
    monkeypatch.setattr(
        memory_mcp,
        "_fresh_source_names",
        lambda selected, ttl_by_source: set(memory_mcp.DEFAULT_SOURCES),
    )

    def unexpected_collect(sources, publish=False):
        raise AssertionError(f"fresh source should not run: {sources}")

    monkeypatch.setattr(context_collector, "collect_selected", unexpected_collect)
    result = memory_mcp.refresh_context(publish_observability=False)

    assert result["exit_code"] == 0
    assert result["refreshed_sources"] == []
    assert result["skipped_sources"] == list(memory_mcp.DEFAULT_SOURCES)
    assert all(item["skipped"] for item in result["result"]["sources"].values())


def test_targeted_refresh_forces_requested_source_by_default(monkeypatch) -> None:
    calls: list[str] = []

    def fake_collect(sources, publish=False):
        calls.extend(sources)
        return {sources[0]: {"ok": True}}

    monkeypatch.setattr(context_collector, "collect_selected", fake_collect)
    result = memory_mcp.refresh_context(["project"], publish_observability=False)

    assert calls == ["project"]
    assert result["refreshed_sources"] == ["project"]
    assert result["skipped_sources"] == []


def test_codex_refresh_is_explicit_change_memory_noop(monkeypatch) -> None:
    def unexpected_collect(sources, publish=False):
        raise AssertionError(f"managed source should not run a collector: {sources}")

    monkeypatch.setattr(context_collector, "collect_selected", unexpected_collect)
    result = memory_mcp.refresh_context(["codex"], publish_observability=False)

    assert result["exit_code"] == 0
    assert result["refreshed_sources"] == []
    assert result["skipped_sources"] == ["codex"]
    assert result["result"]["sources"]["codex"] == {
        "ok": True,
        "skipped": True,
        "managed": True,
        "reason": "Change Memory is updated by record_change; no snapshot collector is required",
        "duration_seconds": 0.0,
    }


def test_mixed_refresh_collects_snapshots_and_preserves_codex_noop(monkeypatch) -> None:
    calls: list[str] = []

    def fake_collect(sources, publish=False):
        calls.extend(sources)
        return {sources[0]: {"ok": True}}

    monkeypatch.setattr(context_collector, "collect_selected", fake_collect)
    result = memory_mcp.refresh_context(
        ["PROJECT", "codex", "tools"],
        publish_observability=False,
        skip_fresh=False,
    )

    assert result["exit_code"] == 0
    assert set(calls) == {"project", "tools"}
    assert result["refreshed_sources"] == ["project", "tools"]
    assert result["skipped_sources"] == ["codex"]


def test_unknown_refresh_source_fails_before_any_collection(monkeypatch) -> None:
    def unexpected_collect(sources, publish=False):
        raise AssertionError(f"invalid request must not partially refresh: {sources}")

    monkeypatch.setattr(context_collector, "collect_selected", unexpected_collect)
    result = memory_mcp.refresh_context(
        ["project", "not-a-source"],
        publish_observability=False,
        skip_fresh=False,
    )

    assert result["exit_code"] == 1
    assert result["mode"] == "invalid-request"
    assert result["unknown_sources"] == ["not-a-source"]
    assert "codex" in result["supported_sources"]
    assert result["result"]["sources"] == {}


def test_collector_reports_managed_codex_source_as_noop() -> None:
    result = context_collector.collect_selected(["codex"], publish=False)

    assert result["codex"]["ok"] is True
    assert result["codex"]["skipped"] is True
    assert result["codex"]["managed"] is True
