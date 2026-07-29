from __future__ import annotations

import agent_orchestrator


def test_plan_branches_is_lightweight_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(agent_orchestrator, "BRANCH_DIR", tmp_path / "branches")
    monkeypatch.setattr(agent_orchestrator, "LOG", tmp_path / "agent_branches.jsonl")

    called = False

    def fake_prompt(branch_id: str) -> dict[str, str]:
        nonlocal called
        called = True
        return {"branch_id": branch_id, "message": "prompt"}

    monkeypatch.setattr(agent_orchestrator, "build_agent_prompt", fake_prompt)

    result = agent_orchestrator.plan_branches(
        "Improve MCP",
        [{"title": "Tests", "goal": "Add tests", "write_scope": ["tests"]}],
    )

    assert result["prompts_included"] is False
    assert "prompts" not in result
    assert called is False
    assert result["branches"][0]["write_scope"] == ["tests"]


def test_plan_branches_can_include_prompts(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(agent_orchestrator, "BRANCH_DIR", tmp_path / "branches")
    monkeypatch.setattr(agent_orchestrator, "LOG", tmp_path / "agent_branches.jsonl")
    monkeypatch.setattr(
        agent_orchestrator,
        "build_agent_prompt",
        lambda branch_id: {"branch_id": branch_id, "message": "prompt"},
    )

    result = agent_orchestrator.plan_branches(
        "Improve MCP",
        [{"title": "Tests", "goal": "Add tests", "write_scope": ["tests"]}],
        include_prompts=True,
    )

    assert result["prompts_included"] is True
    assert result["prompts"][0]["message"] == "prompt"


def test_integration_summary_excludes_terminal_branch_statuses(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(agent_orchestrator, "BRANCH_DIR", tmp_path / "branches")
    monkeypatch.setattr(agent_orchestrator, "LOG", tmp_path / "agent_branches.jsonl")

    planned = agent_orchestrator.create_branch(title="Open", goal="Still active")
    completed = agent_orchestrator.create_branch(title="Done", goal="Already finished")
    superseded = agent_orchestrator.create_branch(title="Old plan", goal="Replaced")
    failed = agent_orchestrator.create_branch(title="Failed", goal="Could not complete")
    canceled = agent_orchestrator.create_branch(title="Canceled", goal="Stopped")

    agent_orchestrator.record_result(
        branch_id=completed["branch_id"],
        status="completed",
        summary="Finished successfully.",
    )
    agent_orchestrator.record_result(
        branch_id=superseded["branch_id"],
        status="superseded",
        summary="Replaced by a newer implementation.",
    )
    agent_orchestrator.record_result(
        branch_id=failed["branch_id"],
        status="failed",
        summary="Failed during verification.",
    )
    agent_orchestrator.record_result(
        branch_id=canceled["branch_id"],
        status="canceled",
        summary="Canceled before launch.",
    )

    summary = agent_orchestrator.integration_summary()

    assert summary["branch_count"] == 5
    assert summary["result_count"] == 4
    assert [branch["branch_id"] for branch in summary["open"]] == [planned["branch_id"]]
