from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mcp_extensions import (
    APP_MIME_TYPE,
    AppPilot,
    CodexWorkersTaskAdapter,
    DurableTaskStore,
    UnsupportedWireExtension,
    app_tool_result,
    bind_app_tool,
    build_apps_extension,
    extension_support,
    tasks_wire_extension,
)


def test_feature_detection_is_truthful_for_mcp_2() -> None:
    support = extension_support()

    assert support["apps"]["wire_supported"] is True
    assert support["apps"]["mime_type"] == APP_MIME_TYPE
    assert support["tasks"]["protocol_models_available"] is True
    assert support["tasks"]["server_extension_available"] is False
    assert support["tasks"]["wire_supported"] is False
    assert support["tasks"]["mode"] == "internal-only"

    with pytest.raises(UnsupportedWireExtension, match="no mcp.server.tasks"):
        tasks_wire_extension()


def test_durable_store_lifecycle_and_restart_read(tmp_path) -> None:
    store = DurableTaskStore(tmp_path)
    created = store.create(
        backend_kind="codex_workers.batch",
        backend_ref="batch-1",
        task_id="task-1",
        ttl_ms=10_000,
    )
    assert created["wire_supported"] is False

    waiting = store.request_input("task-1", {"approval": {"kind": "elicitation"}})
    assert waiting["status"] == "input_required"
    assert "approval" in waiting["input_requests"]

    resumed = DurableTaskStore(tmp_path).resume("task-1")
    assert resumed["status"] == "working"
    assert resumed["input_requests"] is None

    completed = store.complete("task-1", {"ok": True})
    assert completed["status"] == "completed"
    assert completed["progress"] == 1.0
    assert DurableTaskStore(tmp_path).get("task-1")["result"] == {"ok": True}

    with pytest.raises(ValueError, match="terminal task"):
        store.update("task-1", status="working")


def test_store_rejects_path_traversal_and_detects_expiry(tmp_path) -> None:
    store = DurableTaskStore(tmp_path)
    with pytest.raises(ValueError, match="unsafe task id"):
        store.create(
            backend_kind="worker",
            backend_ref="x",
            task_id="../escape",
        )

    record = store.create(
        backend_kind="worker",
        backend_ref="x",
        task_id="expires",
        ttl_ms=1,
    )
    future = datetime.now(UTC) + timedelta(seconds=1)
    assert store.is_expired(record, now=future) is True


def test_codex_workers_adapter_maps_batch_to_durable_task(tmp_path) -> None:
    state = {"finished": False}

    def submit(tasks, max_parallel=None):
        assert tasks == [{"name": "probe"}]
        assert max_parallel == 1
        return {"batch_id": "batch-42", "status": "running"}

    def status(batch_id):
        assert batch_id == "batch-42"
        if state["finished"]:
            return {
                "batch_id": batch_id,
                "status": "finished",
                "counts": {"total": 1, "finished": 1, "failed": 0},
            }
        return {
            "batch_id": batch_id,
            "status": "running",
            "counts": {"total": 1, "finished": 0, "failed": 0},
        }

    def results(batch_id):
        return {"batch_id": batch_id, "status": "finished", "results": [{"ok": True}]}

    adapter = CodexWorkersTaskAdapter(
        DurableTaskStore(tmp_path),
        submit_tasks=submit,
        batch_status=status,
        batch_results=results,
    )
    task = adapter.submit([{"name": "probe"}], max_parallel=1)
    assert task["backend"]["ref"] == "batch-42"
    assert adapter.refresh(task["task_id"])["status"] == "working"

    state["finished"] = True
    finished = adapter.refresh(task["task_id"])
    assert finished["status"] == "completed"
    assert finished["result"]["batch_id"] == "batch-42"
    assert adapter.descriptor()["backend_restart_recovery"] is False


def test_cancellation_does_not_claim_success_without_backend_confirmation(tmp_path) -> None:
    store = DurableTaskStore(tmp_path)
    task = store.create(backend_kind="codex_workers.batch", backend_ref="batch-1")
    adapter = CodexWorkersTaskAdapter(
        store,
        submit_tasks=lambda *args, **kwargs: {},
        batch_status=lambda batch_id: {},
        batch_results=lambda batch_id: {},
    )

    requested = adapter.request_cancel(task["task_id"])
    assert requested["status"] == "working"
    assert requested["cancel_requested"] is True
    assert "awaiting backend confirmation" in requested["status_message"]


def test_apps_extension_binding_and_text_fallback() -> None:
    pilot = AppPilot(
        resource_uri="ui://audit/dashboard.html",
        html="<html><body><main id='app'>Audit</main></body></html>",
        fallback_text="Audit completed: no findings.",
        title="Audit dashboard",
    )
    apps = build_apps_extension([pilot])
    assert apps is not None
    resources = list(apps.resources())
    assert resources[0].resource.mime_type == APP_MIME_TYPE

    def audit() -> dict:
        return app_tool_result(pilot=pilot, data={"findings": 0})

    bind_app_tool(apps, pilot, audit, description="Render the audit dashboard")
    tool = list(apps.tools())[0]
    assert tool.meta["ui"]["resourceUri"] == pilot.resource_uri

    fallback = audit()
    assert fallback["summary"] == "Audit completed: no findings."
    assert fallback["presentation"]["mode"] == "text"
    assert fallback["presentation"]["resource_uri"] is None

    interactive = app_tool_result(
        pilot=pilot,
        data={"findings": 0},
        client_supports_ui=True,
    )
    assert interactive["presentation"]["mode"] == "mcp-app"
    assert interactive["presentation"]["mime_type"] == APP_MIME_TYPE


@pytest.mark.parametrize(
    ("resource_uri", "html", "fallback"),
    [
        ("https://example.test/app", "<html></html>", "fallback"),
        ("ui://audit/app", "", "fallback"),
        ("ui://audit/app", "<html></html>", ""),
    ],
)
def test_app_definition_requires_safe_fallback(
    resource_uri: str,
    html: str,
    fallback: str,
) -> None:
    with pytest.raises(ValueError):
        AppPilot(
            resource_uri=resource_uri,
            html=html,
            fallback_text=fallback,
        )
