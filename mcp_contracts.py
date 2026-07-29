"""Reusable structured-output contracts for MCPServer tools.

Returning one of these Pydantic models from an ``@mcp.tool()`` function lets
MCPServer derive an ``outputSchema`` and emit validated ``structuredContent``.
The helpers in this module keep schemas on JSON Schema draft 2020-12 and
redact common credential shapes before payload validation.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode


JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"
REDACTED = "[redacted]"

_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|cookie|credential|"
    r"pass(?:word|wd)?|private[_-]?key|secret|session|token)(?:$|[_-])",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"
)
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*([:=])\s*"
    r"([^\s,;&]+)"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://[^/\s:@]+:)[^@/\s]+(@)")


def redact_sensitive(value: Any, *, key_hint: str = "") -> Any:
    """Return a recursively redacted JSON-like copy without mutating ``value``.

    Key names are checked before values so an entire credential field is
    replaced even when it contains a nested object. Common inline
    ``Authorization`` headers, assignments, and URL passwords are also
    removed. This is a defensive output boundary, not a substitute for keeping
    secrets out of tool results in the first place.
    """

    if key_hint and _SENSITIVE_KEY_RE.search(key_hint):
        return REDACTED
    if isinstance(value, dict):
        return {
            str(key): redact_sensitive(item, key_hint=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item, key_hint=key_hint) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item, key_hint=key_hint) for item in value]
    if isinstance(value, str):
        text = _AUTHORIZATION_RE.sub(r"\1[redacted]", value)
        text = _ASSIGNMENT_RE.sub(r"\1\2[redacted]", text)
        return _URL_CREDENTIAL_RE.sub(r"\1[redacted]\2", text)
    return value


class ContractModel(BaseModel):
    """Base for deterministic, closed MCP structured-output contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: str = "any_of",
    ) -> dict[str, Any]:
        """Return a self-identifying JSON Schema draft 2020-12 document."""

        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,  # type: ignore[arg-type]
        )
        return {"$schema": JSON_SCHEMA_2020_12, **schema}


class HealthState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class ServiceHealth(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    status: HealthState
    reachable: bool | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    status_code: int | None = Field(default=None, ge=100, le=599)
    message: str | None = Field(default=None, max_length=1000)


class HealthCounts(ContractModel):
    ok: int = Field(default=0, ge=0)
    degraded: int = Field(default=0, ge=0)
    down: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.ok + self.degraded + self.down + self.unknown


class HealthReport(ContractModel):
    service: str = Field(min_length=1, max_length=128)
    status: HealthState
    checked_at: datetime
    checks: list[ServiceHealth] = Field(default_factory=list)
    counts: HealthCounts

    @model_validator(mode="after")
    def counts_match_checks(self) -> HealthReport:
        actual = HealthCounts(
            ok=sum(check.status is HealthState.OK for check in self.checks),
            degraded=sum(check.status is HealthState.DEGRADED for check in self.checks),
            down=sum(check.status is HealthState.DOWN for check in self.checks),
            unknown=sum(check.status is HealthState.UNKNOWN for check in self.checks),
        )
        if actual != self.counts:
            raise ValueError("health counts do not match checks")
        return self


class AuditSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AuditFinding(ContractModel):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    severity: AuditSeverity
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    resource: str | None = Field(default=None, max_length=500)
    remediation: str | None = Field(default=None, max_length=2000)


class AuditCounts(ContractModel):
    info: int = Field(default=0, ge=0)
    low: int = Field(default=0, ge=0)
    medium: int = Field(default=0, ge=0)
    high: int = Field(default=0, ge=0)
    critical: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.info + self.low + self.medium + self.high + self.critical


class AuditReport(ContractModel):
    subject: str = Field(min_length=1, max_length=200)
    passed: bool
    checked_at: datetime
    findings: list[AuditFinding] = Field(default_factory=list)
    counts: AuditCounts

    @model_validator(mode="after")
    def counts_match_findings(self) -> AuditReport:
        actual = AuditCounts(
            **{
                severity.value: sum(
                    finding.severity is severity for finding in self.findings
                )
                for severity in AuditSeverity
            }
        )
        if actual != self.counts:
            raise ValueError("audit counts do not match findings")
        if self.passed and (self.counts.high or self.counts.critical):
            raise ValueError("a passing audit cannot contain high or critical findings")
        return self


class WorkerState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class WorkerResult(ContractModel):
    task_id: str = Field(min_length=1, max_length=128)
    status: WorkerState
    summary: str | None = Field(default=None, max_length=4000)
    duration_ms: float | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=128)
    artifacts: list[str] = Field(default_factory=list, max_length=100)


class WorkerBatchReport(ContractModel):
    batch_id: str = Field(min_length=1, max_length=128)
    status: WorkerState
    submitted: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    results: list[WorkerResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def totals_are_consistent(self) -> WorkerBatchReport:
        if self.completed + self.failed > self.submitted:
            raise ValueError("completed plus failed cannot exceed submitted")
        if len(self.results) > self.submitted:
            raise ValueError("result count cannot exceed submitted")
        return self


class TasksExtensionSupport(ContractModel):
    extension_id: str
    protocol_models_available: bool
    server_extension_available: bool
    wire_supported: bool
    internal_adapter_available: bool
    mode: str


class AppsExtensionSupport(ContractModel):
    extension_id: str
    mime_type: str
    server_extension_available: bool
    wire_supported: bool
    text_fallback_required: bool
    mode: str


class MCPFeatureSupportReport(ContractModel):
    tasks: TasksExtensionSupport
    apps: AppsExtensionSupport


class AppPresentation(ContractModel):
    mode: str
    resource_uri: str | None = None
    mime_type: str


class AppToolResult(ContractModel):
    summary: str = Field(min_length=1, max_length=2000)
    data: dict[str, Any] = Field(default_factory=dict)
    presentation: AppPresentation


ContractT = TypeVar("ContractT", bound=ContractModel)


def validate_structured(
    model: type[ContractT],
    payload: Any,
    *,
    redact: bool = True,
) -> ContractT:
    """Redact and validate a payload against a structured-output contract."""

    candidate = redact_sensitive(payload) if redact else payload
    return model.model_validate(candidate)


def to_structured_content(value: ContractModel) -> dict[str, Any]:
    """Serialize a validated contract exactly as MCP ``structuredContent``."""

    return value.model_dump(mode="json", by_alias=True)


def output_schema(model: type[ContractModel]) -> dict[str, Any]:
    """Return the JSON Schema document MCPServer can expose as ``outputSchema``."""

    return model.model_json_schema(mode="serialization")
