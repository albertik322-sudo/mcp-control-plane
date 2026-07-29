from __future__ import annotations

import importlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Any

import httpx
from mcp_runtime import configure_stdio, run_powershell as _run_powershell


configure_stdio()
ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
LOG = RUNTIME / "worker_batches.jsonl"

MAX_WORKERS = 8
MAX_RESULT_CHARS = 80_000

_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="codex-worker")
_LOCK = threading.RLock()
_BATCHES: dict[str, dict[str, Any]] = {}
_TASKS: dict[str, dict[str, Any]] = {}
_FUTURES: dict[str, Future] = {}
_SEMAPHORES: dict[str, threading.Semaphore] = {}
_BRANCHES: dict[str, dict[str, Any]] = {}

BRANCH_ROOT = RUNTIME / "worker_branches"

ALLOWED_MODULES = {
    "server",
    "docker_mcp",
    "homelab_mcp",
    "homeassistant_mcp",
    "mikrotik_mcp",
    "windows_system_mcp",
    "comfy_mcp",
    "memory_mcp",
    "agent_orchestrator",
}


RECIPES: dict[str, list[dict[str, Any]]] = {
    "server_quick": [
        {"name": "server_overview", "type": "module_call", "module": "server", "function": "system_overview"},
        {"name": "server_services", "type": "module_call", "module": "server", "function": "service_status"},
        {"name": "server_apt", "type": "module_call", "module": "server", "function": "apt_summary"},
        {"name": "server_ports", "type": "module_call", "module": "server", "function": "ports_list"},
    ],
    "homelab_quick": [
        {"name": "homelab_health", "type": "module_call", "module": "homelab_mcp", "function": "homelab_health"},
        {"name": "docker_ps", "type": "module_call", "module": "docker_mcp", "function": "docker_ps"},
        {"name": "docker_stats", "type": "module_call", "module": "docker_mcp", "function": "docker_stats"},
    ],
    "homelab_doc_drift": [
        {"name": "refresh_server", "type": "context_refresh", "sources": ["server"]},
        {"name": "refresh_docker", "type": "context_refresh", "sources": ["docker"]},
        {"name": "refresh_homelab", "type": "context_refresh", "sources": ["homelab"]},
        {"name": "doc_drift_check", "type": "module_call", "module": "homelab_mcp", "function": "doc_drift_check"},
    ],
    "smart_home_quick": [
        {"name": "ha_health", "type": "module_call", "module": "homeassistant_mcp", "function": "health"},
        {"name": "ha_states", "type": "module_call", "module": "homeassistant_mcp", "function": "states"},
        {"name": "ha_yandex", "type": "module_call", "module": "homeassistant_mcp", "function": "yandex_entities"},
    ],
    "network_quick": [
        {"name": "windows_network", "type": "module_call", "module": "windows_system_mcp", "function": "network_summary"},
        {"name": "windows_wifi", "type": "module_call", "module": "windows_system_mcp", "function": "wifi_status"},
        {"name": "mikrotik_snapshot", "type": "module_call", "module": "mikrotik_mcp", "function": "safe_snapshot"},
        {"name": "mikrotik_wifi", "type": "module_call", "module": "mikrotik_mcp", "function": "wifi_registrations"},
    ],
    "context_refresh_light": [
        {"name": "refresh_project", "type": "context_refresh", "sources": ["project"]},
        {"name": "refresh_windows", "type": "context_refresh", "sources": ["windows"]},
        {"name": "refresh_server", "type": "context_refresh", "sources": ["server"]},
    ],
}


def now() -> float:
    return time.time()


def summarize(value: Any, max_chars: int = MAX_RESULT_CHARS) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    if len(text) <= max_chars:
        return value
    return {
        "truncated": True,
        "chars": len(text),
        "preview": text[:max_chars],
    }


def log_event(event: dict[str, Any]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _slug(value: str, fallback: str = "task") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or fallback)[:48]


def _powershell(command: str, timeout: int = 60) -> dict[str, Any]:
    started = now()
    result = _run_powershell(command, timeout=timeout, cwd=str(ROOT))
    result["duration_seconds"] = round(now() - started, 3)
    return result


def _powershell_in(cwd: Path, command: str, timeout: int = 60) -> dict[str, Any]:
    started = now()
    result = _run_powershell(command, timeout=timeout, cwd=str(cwd))
    result["duration_seconds"] = round(now() - started, 3)
    return result


def _validate_write_scopes(scopes: list[str]) -> list[str]:
    if not scopes:
        raise ValueError("write_scopes cannot be empty")
    clean = []
    for scope in scopes:
        item = str(scope).replace("\\", "/").strip().strip("/")
        if not item or item == "." or item.startswith("../") or "/../" in item or item == "..":
            raise ValueError(f"unsafe write scope: {scope}")
        if Path(item).is_absolute():
            raise ValueError(f"write scope must be relative to the repository: {scope}")
        clean.append(item)
    return sorted(set(clean))


def _parse_status_paths(porcelain: str) -> list[str]:
    paths: list[str] = []
    for line in porcelain.splitlines():
        if not line:
            continue
        raw_path = line[3:] if len(line) > 3 else line
        parts = [part.strip().strip('"') for part in raw_path.split(" -> ")]
        paths.extend(part for part in parts if part)
    return sorted(set(paths))


def _scope_violations(paths: list[str], scopes: list[str]) -> list[str]:
    violations = []
    for path in paths:
        normalized = path.replace("\\", "/").strip().strip("/")
        allowed = any(normalized == scope or normalized.startswith(scope.rstrip("/") + "/") for scope in scopes)
        if not allowed:
            violations.append(path)
    return violations


def _branch_dir(branch_id: str) -> Path:
    return BRANCH_ROOT / branch_id


def _branch_paths(branch_id: str) -> dict[str, Path]:
    base = _branch_dir(branch_id)
    return {
        "base": base,
        "worktree": base / "worktree",
        "artifacts": base / "artifacts",
        "manifest": base / "manifest.json",
        "result": base / "result.json",
    }


def _load_branch_record(branch_id: str) -> dict[str, Any]:
    paths = _branch_paths(branch_id)
    manifest = _read_json(paths["manifest"]) if paths["manifest"].exists() else {}
    result = _read_json(paths["result"]) if paths["result"].exists() else None
    record = {
        "branch_id": branch_id,
        "status": "finished" if result else manifest.get("status", "unknown"),
        "manifest": manifest,
        "result": result,
        "paths": {key: str(value) for key, value in paths.items()},
    }
    with _LOCK:
        record.update(_BRANCHES.get(branch_id, {}))
    return record


def _implementation_branch(task: dict[str, Any]) -> dict[str, Any]:
    branch_id = str(task["branch_id"])
    name = str(task["name"])
    command = str(task["command"])
    timeout = int(task.get("timeout", 1200))
    base_ref = str(task.get("base_ref") or "HEAD")
    write_scopes = _validate_write_scopes([str(scope) for scope in task.get("write_scopes", [])])
    branch_name = str(task.get("branch_name") or f"codex/worker/{_slug(name)}-{branch_id[:8]}")
    paths = _branch_paths(branch_id)
    paths["base"].mkdir(parents=True, exist_ok=True)
    paths["artifacts"].mkdir(parents=True, exist_ok=True)
    manifest = {
        "branch_id": branch_id,
        "name": name,
        "type": "implementation_branch",
        "status": "running",
        "created_at": task.get("created_at", now()),
        "started_at": now(),
        "base_ref": base_ref,
        "branch_name": branch_name,
        "write_scopes": write_scopes,
        "paths": {key: str(value) for key, value in paths.items()},
        "command": command,
    }
    _write_json(paths["manifest"], manifest)
    log_event({"event": "implementation_branch_started", "branch_id": branch_id, "branch_name": branch_name, "created_at": manifest["started_at"]})

    add_command = "git worktree add -b {branch} {worktree} {base}".format(
        branch=_ps_quote(branch_name),
        worktree=_ps_quote(paths["worktree"]),
        base=_ps_quote(base_ref),
    )
    add_result = _powershell_in(ROOT, add_command, timeout=120)
    if add_result.get("exit_code") != 0:
        result = {
            "ok": False,
            "stage": "worktree_add",
            "branch_id": branch_id,
            "branch_name": branch_name,
            "manifest_path": str(paths["manifest"]),
            "artifact_dir": str(paths["artifacts"]),
            "worktree": str(paths["worktree"]),
            "add_result": summarize(add_result),
        }
        manifest["status"] = "failed"
        manifest["finished_at"] = now()
        _write_json(paths["manifest"], manifest)
        _write_json(paths["result"], result)
        return result

    wrapped_command = "\n".join(
        [
            f"$env:CODEX_WORKER_BRANCH_ID={_ps_quote(branch_id)}",
            f"$env:CODEX_WORKER_ARTIFACTS={_ps_quote(paths['artifacts'])}",
            f"$env:CODEX_WORKER_MANIFEST={_ps_quote(paths['manifest'])}",
            command,
        ]
    )
    run_result = _powershell_in(paths["worktree"], wrapped_command, timeout=timeout)
    status_result = _powershell_in(paths["worktree"], "git status --porcelain=v1", timeout=60)
    changed_files = _parse_status_paths(str(status_result.get("stdout") or ""))
    violations = _scope_violations(changed_files, write_scopes)
    artifact_files = sorted(
        str(path.relative_to(paths["artifacts"])) for path in paths["artifacts"].rglob("*") if path.is_file()
    )
    ok = run_result.get("exit_code") == 0 and not violations
    result = {
        "ok": ok,
        "branch_id": branch_id,
        "branch_name": branch_name,
        "base_ref": base_ref,
        "worktree": str(paths["worktree"]),
        "manifest_path": str(paths["manifest"]),
        "result_path": str(paths["result"]),
        "artifact_dir": str(paths["artifacts"]),
        "artifact_files": artifact_files,
        "write_scopes": write_scopes,
        "changed_files": changed_files,
        "scope_violations": violations,
        "run_result": summarize(run_result),
        "status_result": summarize(status_result),
    }
    manifest["status"] = "finished" if ok else "failed"
    manifest["finished_at"] = now()
    _write_json(paths["manifest"], manifest)
    _write_json(paths["result"], result)
    log_event({"event": "implementation_branch_finished", "branch_id": branch_id, "ok": ok, "finished_at": manifest["finished_at"]})
    return result


def _http_probe(urls: list[str], timeout: float = 10.0) -> list[dict[str, Any]]:
    rows = []
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for url in urls:
            started = now()
            try:
                response = client.get(url)
                rows.append(
                    {
                        "url": url,
                        "ok": response.status_code < 500,
                        "status_code": response.status_code,
                        "headers": dict(response.headers),
                        "duration_seconds": round(now() - started, 3),
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "url": url,
                        "ok": False,
                        "error": str(exc),
                        "type": type(exc).__name__,
                        "duration_seconds": round(now() - started, 3),
                    }
                )
    return rows


def _clean_search_url(href: str) -> str:
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return href


def _web_search(query: str, max_results: int = 8, timeout: float = 10.0) -> dict[str, Any]:
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 CodexWorker/1.0",
        "Accept": "text/html,application/xhtml+xml",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.post(url, data={"q": query})
    response.raise_for_status()
    html = response.text
    matches = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.I | re.S)
    results = []
    seen = set()
    for href, title_html in matches:
        clean_url = _clean_search_url(href.replace("&amp;", "&"))
        title = re.sub(r"<.*?>", "", title_html)
        title = re.sub(r"\s+", " ", title).strip()
        if not clean_url or clean_url in seen:
            continue
        seen.add(clean_url)
        results.append({"title": title, "url": clean_url})
        if len(results) >= max(1, min(max_results, 20)):
            break
    return {"query": query, "results": results, "source": "duckduckgo_html"}


def _web_fetch(url: str, timeout: float = 10.0, max_chars: int = 20_000) -> dict[str, Any]:
    headers = {"User-Agent": "Mozilla/5.0 CodexWorker/1.0"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
    content_type = response.headers.get("content-type", "")
    text = response.text if "text" in content_type or "html" in content_type or "json" in content_type else ""
    if text:
        text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
        text = re.sub(r"(?s)<.*?>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return {
        "url": str(response.url),
        "status_code": response.status_code,
        "content_type": content_type,
        "text": text[: max(1, min(max_chars, MAX_RESULT_CHARS))],
        "chars": len(text),
    }


def _module_call(module_name: str, function_name: str, kwargs: dict[str, Any] | None = None) -> Any:
    if module_name not in ALLOWED_MODULES:
        raise ValueError(f"module is not allowed: {module_name}")
    if function_name.startswith("_"):
        raise ValueError("private functions are not callable")
    module = importlib.import_module(module_name)
    fn = getattr(module, function_name)
    if not callable(fn):
        raise ValueError(f"not callable: {module_name}.{function_name}")
    return fn(**(kwargs or {}))


def _context_refresh(sources: list[str] | None = None) -> Any:
    import context_collector

    return context_collector.collect_selected(sources)


def execute_task(task: dict[str, Any]) -> dict[str, Any]:
    started = now()
    task_type = task.get("type")
    try:
        if task_type == "local_powershell":
            result = _powershell(str(task["command"]), int(task.get("timeout", 60)))
        elif task_type == "http_probe":
            urls = task.get("urls") or [task["url"]]
            result = _http_probe([str(url) for url in urls], float(task.get("timeout", 10)))
        elif task_type == "web_search":
            result = _web_search(str(task["query"]), int(task.get("max_results", 8)), float(task.get("timeout", 10)))
        elif task_type == "web_fetch":
            result = _web_fetch(str(task["url"]), float(task.get("timeout", 10)), int(task.get("max_chars", 20_000)))
        elif task_type == "module_call":
            result = _module_call(str(task["module"]), str(task["function"]), task.get("kwargs") or {})
        elif task_type == "context_refresh":
            result = _context_refresh(task.get("sources"))
        elif task_type == "memory_search":
            result = _module_call("memory_mcp", "search_memory", {"query": str(task["query"]), "limit": int(task.get("limit", 20))})
        elif task_type == "implementation_branch":
            result = _implementation_branch(task)
        elif task_type == "agent_branch_prompt":
            result = _module_call("agent_orchestrator", "build_agent_prompt", {"branch_id": str(task["branch_id"])})
        elif task_type == "agent_branch_result":
            result = _module_call(
                "agent_orchestrator",
                "record_result",
                {
                    "branch_id": str(task["branch_id"]),
                    "status": str(task.get("status") or "completed"),
                    "summary": str(task.get("summary") or ""),
                    "changed_files": task.get("changed_files") or [],
                    "verification": task.get("verification") or [],
                    "artifacts": task.get("artifacts") or [],
                    "notes": task.get("notes") or [],
                },
            )
        else:
            raise ValueError(f"unknown task type: {task_type}")
        return {
            "ok": True,
            "result": summarize(result),
            "duration_seconds": round(now() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "type": type(exc).__name__,
            "duration_seconds": round(now() - started, 3),
        }


def _run_task(task_id: str, task: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        _TASKS[task_id]["status"] = "running"
        _TASKS[task_id]["started_at"] = now()
    result = execute_task(task)
    with _LOCK:
        _TASKS[task_id]["status"] = "finished"
        _TASKS[task_id]["finished_at"] = now()
        _TASKS[task_id]["result"] = result
    return result


def _run_task_limited(batch_id: str, task_id: str, task: dict[str, Any]) -> dict[str, Any]:
    semaphore = _SEMAPHORES[batch_id]
    with semaphore:
        return _run_task(task_id, task)


def submit_tasks(tasks: list[dict[str, Any]], max_parallel: int | None = None) -> dict[str, Any]:
    if not tasks:
        raise ValueError("tasks cannot be empty")
    batch_id = uuid.uuid4().hex
    task_ids = []
    created = now()
    parallelism = max(1, min(MAX_WORKERS, int(max_parallel or MAX_WORKERS)))
    with _LOCK:
        _SEMAPHORES[batch_id] = threading.Semaphore(parallelism)
        _BATCHES[batch_id] = {
            "batch_id": batch_id,
            "status": "running",
            "created_at": created,
            "task_ids": task_ids,
            "max_parallel": parallelism,
        }
        for index, task in enumerate(tasks):
            task_id = uuid.uuid4().hex
            task_ids.append(task_id)
            task_name = task.get("name") or f"{task.get('type', 'task')}_{index + 1}"
            _TASKS[task_id] = {
                "task_id": task_id,
                "batch_id": batch_id,
                "name": task_name,
                "status": "queued",
                "created_at": created,
                "task": task,
            }
            future = _EXECUTOR.submit(_run_task_limited, batch_id, task_id, task)
            _FUTURES[task_id] = future
    log_event({"event": "batch_submitted", "batch_id": batch_id, "task_count": len(tasks), "created_at": created})
    return batch_status(batch_id)


def batch_status(batch_id: str) -> dict[str, Any]:
    with _LOCK:
        batch = _BATCHES.get(batch_id)
        if not batch:
            raise KeyError(f"unknown batch: {batch_id}")
        tasks = [_TASKS[task_id] for task_id in batch["task_ids"]]
        finished = sum(1 for task in tasks if task["status"] == "finished")
        failed = sum(1 for task in tasks if task.get("result", {}).get("ok") is False)
        running = sum(1 for task in tasks if task["status"] == "running")
        queued = sum(1 for task in tasks if task["status"] == "queued")
        status = "finished" if finished == len(tasks) else "running"
        batch["status"] = status
        return {
            "batch_id": batch_id,
            "status": status,
            "created_at": batch["created_at"],
            "counts": {"total": len(tasks), "finished": finished, "failed": failed, "running": running, "queued": queued},
            "tasks": [
                {
                    "task_id": task["task_id"],
                    "name": task["name"],
                    "status": task["status"],
                    "ok": task.get("result", {}).get("ok"),
                    "duration_seconds": task.get("result", {}).get("duration_seconds"),
                }
                for task in tasks
            ],
        }


def wait_batch(batch_id: str, timeout: float | None = None) -> dict[str, Any]:
    with _LOCK:
        batch = _BATCHES.get(batch_id)
        if not batch:
            raise KeyError(f"unknown batch: {batch_id}")
        futures = [_FUTURES[task_id] for task_id in batch["task_ids"]]
    wait(futures, timeout=timeout)
    return batch_results(batch_id)


def run_tasks(tasks: list[dict[str, Any]], timeout: float | None = 120, max_parallel: int | None = None) -> dict[str, Any]:
    batch = submit_tasks(tasks, max_parallel=max_parallel)
    return wait_batch(batch["batch_id"], timeout=timeout)


def batch_results(batch_id: str) -> dict[str, Any]:
    status = batch_status(batch_id)
    with _LOCK:
        tasks = [_TASKS[task_id] for task_id in _BATCHES[batch_id]["task_ids"]]
        results = [
            {
                "task_id": task["task_id"],
                "name": task["name"],
                "status": task["status"],
                "task": task["task"],
                "result": task.get("result"),
            }
            for task in tasks
        ]
    return {**status, "results": results}


def list_batches(limit: int = 20) -> dict[str, Any]:
    with _LOCK:
        batches = sorted(_BATCHES.values(), key=lambda row: row["created_at"], reverse=True)[:limit]
    return {"batches": [batch_status(batch["batch_id"]) for batch in batches]}


def run_recipe(name: str, wait_timeout: float | None = 120) -> dict[str, Any]:
    tasks = RECIPES.get(name)
    if not tasks:
        raise ValueError(f"unknown recipe: {name}")
    batch = submit_tasks(tasks)
    return wait_batch(batch["batch_id"], wait_timeout)


def recipes() -> dict[str, Any]:
    return {"recipes": sorted(RECIPES), "details": RECIPES}


def start_implementation_branch(
    name: str,
    command: str,
    write_scopes: list[str],
    timeout: int = 1200,
    base_ref: str = "HEAD",
    branch_name: str | None = None,
) -> dict[str, Any]:
    branch_id = uuid.uuid4().hex
    created = now()
    task = {
        "name": name,
        "type": "implementation_branch",
        "branch_id": branch_id,
        "command": command,
        "write_scopes": _validate_write_scopes(write_scopes),
        "timeout": timeout,
        "base_ref": base_ref,
        "branch_name": branch_name,
        "created_at": created,
    }
    batch = submit_tasks([task], max_parallel=1)
    task_id = batch["tasks"][0]["task_id"]
    paths = _branch_paths(branch_id)
    record = {
        "branch_id": branch_id,
        "batch_id": batch["batch_id"],
        "task_id": task_id,
        "name": name,
        "status": "queued",
        "created_at": created,
        "paths": {key: str(value) for key, value in paths.items()},
    }
    with _LOCK:
        _BRANCHES[branch_id] = record
    log_event({"event": "implementation_branch_submitted", "branch_id": branch_id, "batch_id": batch["batch_id"], "created_at": created})
    return implementation_branch_status(branch_id)


def implementation_branch_status(branch_id: str) -> dict[str, Any]:
    with _LOCK:
        record = _BRANCHES.get(branch_id)
    if not record:
        return _load_branch_record(branch_id)
    batch = batch_status(record["batch_id"])
    task = next((item for item in batch["tasks"] if item["task_id"] == record["task_id"]), None)
    disk = _load_branch_record(branch_id)
    status = disk.get("manifest", {}).get("status") or (task or {}).get("status") or record["status"]
    return {
        **record,
        "status": status,
        "batch": batch,
        "task": task,
        "manifest": disk.get("manifest"),
        "has_result": bool(disk.get("result")),
    }


def implementation_branch_results(branch_id: str) -> dict[str, Any]:
    status = implementation_branch_status(branch_id)
    disk = _load_branch_record(branch_id)
    return {
        **status,
        "result": disk.get("result"),
    }


def list_implementation_branches(limit: int = 20) -> dict[str, Any]:
    seen = set()
    rows: list[dict[str, Any]] = []
    with _LOCK:
        branch_ids = list(_BRANCHES)
    for branch_id in branch_ids:
        seen.add(branch_id)
        rows.append(implementation_branch_status(branch_id))
    if BRANCH_ROOT.exists():
        for path in BRANCH_ROOT.iterdir():
            if not path.is_dir() or path.name in seen:
                continue
            rows.append(_load_branch_record(path.name))
    rows.sort(key=lambda row: row.get("created_at") or row.get("manifest", {}).get("created_at") or 0, reverse=True)
    return {"branches": rows[: max(1, min(limit, 100))]}
