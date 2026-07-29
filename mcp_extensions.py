"""Safe, opt-in pilots for MCP Tasks and MCP Apps.

MCP Apps is implemented by the Python SDK and can be advertised on the wire.
The 2026-07-28 Tasks extension has protocol models in ``mcp.types`` but the
Python SDK does not yet ship a server-side extension implementation.  This
module therefore exposes Tasks only as an internal, durable lifecycle adapter;
it must not be passed to ``MCPServer(extensions=...)`` or advertised to clients.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal


TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
APPS_EXTENSION_ID = "io.modelcontextprotocol/ui"
APP_MIME_TYPE = "text/html;profile=mcp-app"

InternalTaskStatus = Literal[
    "working",
    "input_required",
    "completed",
    "failed",
    "cancelled",
]
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class UnsupportedWireExtension(RuntimeError):
    """Raised when code attempts to expose an unsupported extension on the wire."""


def extension_support() -> dict[str, Any]:
    """Return runtime feature detection without importing optional APIs eagerly."""
    apps_module = _optional_import("mcp.server.apps")
    types_module = _optional_import("mcp.types")
    task_models = bool(
        types_module
        and all(
            hasattr(types_module, name)
            for name in ("Task", "CreateTaskResult", "GetTaskResult")
        )
    )
    task_server_module = _optional_import("mcp.server.tasks")
    apps_available = bool(
        apps_module
        and hasattr(apps_module, "Apps")
        and hasattr(apps_module, "client_supports_apps")
    )
    return {
        "tasks": {
            "extension_id": TASKS_EXTENSION_ID,
            "protocol_models_available": task_models,
            "server_extension_available": task_server_module is not None,
            "wire_supported": task_server_module is not None,
            "internal_adapter_available": True,
            "mode": "wire" if task_server_module is not None else "internal-only",
        },
        "apps": {
            "extension_id": APPS_EXTENSION_ID,
            "mime_type": APP_MIME_TYPE,
            "server_extension_available": apps_available,
            "wire_supported": apps_available,
            "text_fallback_required": True,
            "mode": "wire-with-text-fallback" if apps_available else "text-only",
        },
    }


def tasks_wire_extension() -> Any:
    """Return a future SDK Tasks extension, or fail closed.

    This function deliberately does not fabricate an ``Extension`` from the
    legacy task models.  Callers can use it as a feature gate when the SDK gains
    a supported server implementation.
    """
    module = _optional_import("mcp.server.tasks")
    extension_type = getattr(module, "Tasks", None) if module else None
    if extension_type is None:
        raise UnsupportedWireExtension(
            "mcp 2.0.0 has task protocol models but no mcp.server.tasks "
            "implementation; use CodexWorkersTaskAdapter internally"
        )
    return extension_type()


class DurableTaskStore:
    """Small JSON-backed store for internal task handles.

    Each task is stored in a separate file and updated with ``os.replace``.
    The store is process-safe for readers and crash-resistant for individual
    writes.  Cross-process concurrent writers still require an external lock.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        backend_kind: str,
        backend_ref: str,
        status_message: str = "Submitted",
        ttl_ms: int = 86_400_000,
        poll_interval_ms: int = 1_000,
        metadata: Mapping[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        if ttl_ms <= 0 or poll_interval_ms <= 0:
            raise ValueError("ttl_ms and poll_interval_ms must be positive")
        task_id = task_id or uuid.uuid4().hex
        path = self._path(task_id)
        if path.exists():
            raise FileExistsError(f"task already exists: {task_id}")
        timestamp = _utc_now()
        record = {
            "task_id": task_id,
            "status": "working",
            "status_message": status_message,
            "created_at": timestamp,
            "last_updated_at": timestamp,
            "ttl_ms": ttl_ms,
            "poll_interval_ms": poll_interval_ms,
            "backend": {"kind": backend_kind, "ref": backend_ref},
            "metadata": dict(metadata or {}),
            "progress": None,
            "input_requests": None,
            "result": None,
            "error": None,
            "cancel_requested": False,
            "transport": "internal",
            "extension_id": TASKS_EXTENSION_ID,
            "wire_supported": False,
        }
        self._write(path, record)
        return record

    def get(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        if not path.exists():
            raise KeyError(f"unknown task: {task_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self, *, include_expired: bool = False) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if include_expired or not self.is_expired(record):
                records.append(record)
        return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def update(
        self,
        task_id: str,
        *,
        status: InternalTaskStatus | None = None,
        status_message: str | None = None,
        progress: float | None = None,
        input_requests: Mapping[str, Any] | None = None,
        result: Any = None,
        error: Any = None,
        cancel_requested: bool | None = None,
    ) -> dict[str, Any]:
        record = self.get(task_id)
        current = record["status"]
        if current in _TERMINAL_STATUSES and status not in (None, current):
            raise ValueError(f"terminal task {task_id} cannot transition from {current} to {status}")
        if status is not None:
            _validate_transition(current, status)
            record["status"] = status
        if status_message is not None:
            record["status_message"] = status_message
        if progress is not None:
            if not 0 <= progress <= 1:
                raise ValueError("progress must be between 0 and 1")
            record["progress"] = progress
        if input_requests is not None:
            record["input_requests"] = dict(input_requests)
        if result is not None:
            record["result"] = result
        if error is not None:
            record["error"] = error
        if cancel_requested is not None:
            record["cancel_requested"] = cancel_requested
        if record["status"] != "input_required":
            record["input_requests"] = None
        record["last_updated_at"] = _utc_now()
        self._write(self._path(task_id), record)
        return record

    def request_input(
        self,
        task_id: str,
        requests: Mapping[str, Any],
        *,
        message: str = "Additional input required",
    ) -> dict[str, Any]:
        if not requests:
            raise ValueError("requests cannot be empty")
        return self.update(
            task_id,
            status="input_required",
            status_message=message,
            input_requests=requests,
        )

    def resume(self, task_id: str, *, message: str = "Resumed") -> dict[str, Any]:
        return self.update(task_id, status="working", status_message=message)

    def complete(self, task_id: str, result: Any) -> dict[str, Any]:
        return self.update(
            task_id,
            status="completed",
            status_message="Completed",
            progress=1.0,
            result=result,
        )

    def fail(self, task_id: str, error: Any) -> dict[str, Any]:
        return self.update(
            task_id,
            status="failed",
            status_message="Failed",
            error=error,
        )

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        record = self.get(task_id)
        if record["status"] in _TERMINAL_STATUSES:
            return record
        return self.update(
            task_id,
            status_message="Cancellation requested; awaiting backend confirmation",
            cancel_requested=True,
        )

    def confirm_cancelled(self, task_id: str) -> dict[str, Any]:
        return self.update(
            task_id,
            status="cancelled",
            status_message="Cancelled by backend",
            cancel_requested=True,
        )

    @staticmethod
    def is_expired(record: Mapping[str, Any], *, now: datetime | None = None) -> bool:
        created = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00"))
        expires = created + timedelta(milliseconds=int(record["ttl_ms"]))
        return (now or datetime.now(UTC)) >= expires

    def _path(self, task_id: str) -> Path:
        if not _SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError(f"unsafe task id: {task_id!r}")
        return self.root / f"{task_id}.json"

    @staticmethod
    def _write(path: Path, record: Mapping[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, indent=2, default=str)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


class CodexWorkersTaskAdapter:
    """Durable internal handles over the existing codex_workers batch API.

    The adapter persists the MCP-facing lifecycle, while execution remains
    owned by ``codex_workers``.  It does not claim that the backend itself can
    survive a worker-process restart, and does not advertise Tasks on the wire.
    """

    def __init__(
        self,
        store: DurableTaskStore,
        *,
        submit_tasks: Callable[..., Mapping[str, Any]],
        batch_status: Callable[[str], Mapping[str, Any]],
        batch_results: Callable[[str], Mapping[str, Any]],
        cancel_batch: Callable[[str], Any] | None = None,
    ) -> None:
        self.store = store
        self._submit_tasks = submit_tasks
        self._batch_status = batch_status
        self._batch_results = batch_results
        self._cancel_batch = cancel_batch

    def submit(
        self,
        tasks: list[dict[str, Any]],
        *,
        max_parallel: int | None = None,
        ttl_ms: int = 86_400_000,
    ) -> dict[str, Any]:
        snapshot = self._submit_tasks(tasks, max_parallel=max_parallel)
        batch_id = str(snapshot.get("batch_id") or "")
        if not batch_id:
            raise ValueError("codex_workers submit_tasks returned no batch_id")
        return self.store.create(
            backend_kind="codex_workers.batch",
            backend_ref=batch_id,
            ttl_ms=ttl_ms,
            metadata={"task_count": len(tasks), "max_parallel": max_parallel},
        )

    def refresh(self, task_id: str) -> dict[str, Any]:
        record = self.store.get(task_id)
        if record["status"] in _TERMINAL_STATUSES:
            return record
        batch_id = str(record["backend"]["ref"])
        snapshot = dict(self._batch_status(batch_id))
        counts = dict(snapshot.get("counts") or {})
        total = int(counts.get("total") or 0)
        finished = int(counts.get("finished") or 0)
        progress = finished / total if total else None
        if snapshot.get("status") != "finished":
            return self.store.update(
                task_id,
                status="working",
                status_message="Worker batch is running",
                progress=progress,
            )
        results = dict(self._batch_results(batch_id))
        failed = int(counts.get("failed") or 0)
        if failed:
            return self.store.fail(task_id, results)
        return self.store.complete(task_id, results)

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        record = self.store.request_cancel(task_id)
        if self._cancel_batch is None or record["status"] in _TERMINAL_STATUSES:
            return record
        backend_result = self._cancel_batch(str(record["backend"]["ref"]))
        if _backend_confirms_cancelled(backend_result):
            return self.store.confirm_cancelled(task_id)
        return self.store.update(
            task_id,
            status_message="Cancellation requested; backend has not confirmed cancellation",
        )

    def descriptor(self) -> dict[str, Any]:
        return {
            "extension_id": TASKS_EXTENSION_ID,
            "mode": "internal-only",
            "wire_supported": False,
            "durable_handle_store": str(self.store.root),
            "backend": "codex_workers",
            "backend_restart_recovery": False,
            "cancellation_supported": self._cancel_batch is not None,
        }


@dataclass(frozen=True)
class AppPilot:
    """Definition for one UI resource plus its mandatory text fallback."""

    resource_uri: str
    html: str
    fallback_text: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    connect_domains: tuple[str, ...] = ()
    resource_domains: tuple[str, ...] = ()
    frame_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.resource_uri.startswith("ui://"):
            raise ValueError("MCP App resource_uri must use the ui:// scheme")
        if not self.html.strip():
            raise ValueError("MCP App html cannot be empty")
        if not self.fallback_text.strip():
            raise ValueError("MCP App fallback_text cannot be empty")


def build_apps_extension(pilots: Iterable[AppPilot]) -> Any | None:
    """Build the SDK Apps extension, or return ``None`` for text-only mode."""
    module = _optional_import("mcp.server.apps")
    apps_type = getattr(module, "Apps", None) if module else None
    csp_type = getattr(module, "ResourceCsp", None) if module else None
    if apps_type is None or csp_type is None:
        return None
    apps = apps_type()
    for pilot in pilots:
        csp = csp_type(
            connect_domains=list(pilot.connect_domains) or None,
            resource_domains=list(pilot.resource_domains) or None,
            frame_domains=list(pilot.frame_domains) or None,
        )
        apps.add_html_resource(
            pilot.resource_uri,
            pilot.html,
            name=pilot.name,
            title=pilot.title,
            description=pilot.description,
            csp=csp,
        )
    return apps


def bind_app_tool(
    apps: Any,
    pilot: AppPilot,
    fn: Callable[..., Any],
    *,
    visibility: tuple[Literal["model", "app"], ...] = ("model", "app"),
    **tool_kwargs: Any,
) -> Callable[..., Any]:
    """Bind a callable to an app resource through the SDK extension."""
    if apps is None or not hasattr(apps, "tool"):
        raise UnsupportedWireExtension("MCP Apps server support is unavailable")
    return apps.tool(
        resource_uri=pilot.resource_uri,
        visibility=visibility,
        **tool_kwargs,
    )(fn)


def app_tool_result(
    *,
    pilot: AppPilot,
    data: Mapping[str, Any] | None = None,
    client_supports_ui: bool = False,
) -> dict[str, Any]:
    """Return useful structured content for both UI and text-only hosts."""
    return {
        "summary": pilot.fallback_text,
        "data": dict(data or {}),
        "presentation": {
            "mode": "mcp-app" if client_supports_ui else "text",
            "resource_uri": pilot.resource_uri if client_supports_ui else None,
            "mime_type": APP_MIME_TYPE if client_supports_ui else "text/plain",
        },
    }


def _optional_import(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except (ImportError, ModuleNotFoundError):
        return None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _validate_transition(current: str, target: str) -> None:
    allowed = {
        "working": {"working", "input_required", "completed", "failed", "cancelled"},
        "input_required": {"input_required", "working", "failed", "cancelled"},
        "completed": {"completed"},
        "failed": {"failed"},
        "cancelled": {"cancelled"},
    }
    if target not in allowed.get(current, set()):
        raise ValueError(f"invalid task transition: {current} -> {target}")


def _backend_confirms_cancelled(result: Any) -> bool:
    if result is True:
        return True
    if isinstance(result, Mapping):
        return result.get("status") == "cancelled" or result.get("cancelled") is True
    return False
