from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jsonschema.validators import Draft202012Validator
from pydantic import ValidationError

from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp_contracts import (
    JSON_SCHEMA_2020_12,
    REDACTED,
    AuditCounts,
    AuditFinding,
    AuditReport,
    AuditSeverity,
    HealthCounts,
    HealthReport,
    HealthState,
    ServiceHealth,
    WorkerBatchReport,
    WorkerResult,
    WorkerState,
    output_schema,
    redact_sensitive,
    to_structured_content,
    validate_structured,
)


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "model",
    (HealthReport, AuditReport, WorkerBatchReport),
)
def test_contract_schema_is_closed_draft_2020_12(model: type) -> None:
    schema = output_schema(model)

    assert schema["$schema"] == JSON_SCHEMA_2020_12
    assert schema["additionalProperties"] is False
    Draft202012Validator.check_schema(schema)


def test_health_contract_serializes_deterministically() -> None:
    report = HealthReport(
        service="homelab",
        status=HealthState.DEGRADED,
        checked_at=NOW,
        checks=[
            ServiceHealth(name="prometheus", status=HealthState.OK, duration_ms=12.5),
            ServiceHealth(name="grafana", status=HealthState.DOWN, reachable=False),
        ],
        counts=HealthCounts(ok=1, down=1),
    )

    assert to_structured_content(report) == {
        "service": "homelab",
        "status": "degraded",
        "checked_at": "2026-07-29T12:00:00Z",
        "checks": [
            {
                "name": "prometheus",
                "status": "ok",
                "reachable": None,
                "duration_ms": 12.5,
                "status_code": None,
                "message": None,
            },
            {
                "name": "grafana",
                "status": "down",
                "reachable": False,
                "duration_ms": None,
                "status_code": None,
                "message": None,
            },
        ],
        "counts": {"ok": 1, "degraded": 0, "down": 1, "unknown": 0},
    }


def test_audit_contract_enforces_counts_and_passing_threshold() -> None:
    finding = AuditFinding(
        code="mcp.unpinned-dependency",
        severity=AuditSeverity.HIGH,
        title="Dependency is not pinned",
        summary="Use an immutable version.",
    )

    with pytest.raises(ValidationError, match="counts do not match"):
        AuditReport(
            subject="control-plane",
            passed=False,
            checked_at=NOW,
            findings=[finding],
            counts=AuditCounts(),
        )

    with pytest.raises(ValidationError, match="passing audit"):
        AuditReport(
            subject="control-plane",
            passed=True,
            checked_at=NOW,
            findings=[finding],
            counts=AuditCounts(high=1),
        )


def test_worker_contract_rejects_impossible_totals() -> None:
    with pytest.raises(ValidationError, match="cannot exceed submitted"):
        WorkerBatchReport(
            batch_id="batch-1",
            status=WorkerState.FAILED,
            submitted=1,
            completed=1,
            failed=1,
            results=[
                WorkerResult(task_id="task-1", status=WorkerState.COMPLETED)
            ],
        )


def test_redaction_is_recursive_deterministic_and_non_mutating() -> None:
    payload = {
        "token": "top-secret",
        "nested": [
            {"api_key": {"value": "also-secret"}},
            "Authorization: Bearer abc.def.ghi",
            "https://user:password@example.invalid/path",
            "password=hunter2",
        ],
    }

    redacted = redact_sensitive(payload)

    assert redacted == {
        "token": REDACTED,
        "nested": [
            {"api_key": REDACTED},
            "Authorization: [redacted]",
            "https://user:[redacted]@example.invalid/path",
            "password=[redacted]",
        ],
    }
    assert payload["token"] == "top-secret"


def test_validation_redacts_before_errors_can_render_input() -> None:
    secret = "never-render-this-value"
    payload = {
        "service": "context-store",
        "status": "ok",
        "checked_at": NOW,
        "checks": [],
        "counts": {"ok": 0, "degraded": 0, "down": 0, "unknown": 0},
        "password": secret,
    }

    with pytest.raises(ValidationError) as caught:
        validate_structured(HealthReport, payload)

    assert secret not in str(caught.value)


def test_mcpserver_derives_output_schema_and_structured_content_from_model() -> None:
    def health_tool() -> HealthReport:
        return HealthReport(
            service="fixture",
            status=HealthState.OK,
            checked_at=NOW,
            checks=[ServiceHealth(name="self", status=HealthState.OK)],
            counts=HealthCounts(ok=1),
        )

    metadata = func_metadata(health_tool)
    result = metadata.convert_result(health_tool())

    assert metadata.output_schema is not None
    assert metadata.output_schema["$schema"] == JSON_SCHEMA_2020_12
    assert result.structured_content == to_structured_content(health_tool())
