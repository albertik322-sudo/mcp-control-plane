from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
BRANCH_DIR = RUNTIME / "agent_branches"
LOG = RUNTIME / "agent_branches.jsonl"

DEFAULT_CONTEXT_TOOLS = [
    "codex_memory.context_overview",
    "codex_memory.search_memory",
    "codex_memory.get_source_context",
    "codex_memory.list_sources",
]

MCP_ACCESS_RULES = [
    "Start with Context Store before live reads.",
    "Use the relevant MCP server for laptop, server, Docker, homelab, MikroTik, Home Assistant, or tunnel work.",
    "Use direct shell only for local repository edits, verification, or when an MCP server is unavailable.",
    "Do not revert or overwrite changes made by other agents.",
    "Keep write scope disjoint from other branches.",
    "Record meaningful final changes through Change Memory in the parent integration step.",
]

TERMINAL_BRANCH_STATUSES = {
    "cancelled",
    "canceled",
    "complete",
    "completed",
    "done",
    "error",
    "failed",
    "skipped",
    "superseded",
}


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now()))


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-")
    return text[:60] or "agent-branch"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def log_event(event: dict[str, Any]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**event, "ts": now(), "time": iso()}, ensure_ascii=False, default=str) + "\n")


def branch_path(branch_id: str) -> Path:
    return BRANCH_DIR / branch_id


def manifest_path(branch_id: str) -> Path:
    return branch_path(branch_id) / "manifest.json"


def result_path(branch_id: str) -> Path:
    return branch_path(branch_id) / "result.json"


def context_pack_path(branch_id: str) -> Path:
    return branch_path(branch_id) / "context_pack.json"


def _compact(value: Any, max_chars: int = 12000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str, indent=2) if not isinstance(value, str) else value
    if len(text) <= max_chars:
        return value
    return {"truncated": True, "chars": len(text), "preview": text[:max_chars]}


def build_context_pack(branch_id: str, max_search_results: int = 8) -> dict[str, Any]:
    manifest = _read_json(manifest_path(branch_id))
    topics = manifest.get("context_topics") or [manifest.get("title", ""), manifest.get("goal", "")]
    query = " ".join(str(topic) for topic in topics if str(topic).strip()).strip()
    pack: dict[str, Any] = {
        "branch_id": branch_id,
        "generated_at": iso(),
        "workspace": str(ROOT),
        "task": {
            "title": manifest.get("title"),
            "goal": manifest.get("goal"),
            "write_scope": manifest.get("write_scope", []),
            "read_scope": manifest.get("read_scope", []),
            "verification": manifest.get("verification", []),
        },
        "context_store": {},
        "repository": {},
    }
    try:
        import codex_memory as mem

        with mem.connect() as conn:
            pack["context_store"]["freshness"] = mem.freshness(conn)
            pack["context_store"]["recent_changes"] = mem.list_changes(conn, limit=6)
            pack["context_store"]["matches"] = mem.search(conn, query, limit=max(1, min(int(max_search_results), 25))) if query else []
            pack["context_store"]["project_docs"] = [
                {key: row.get(key) for key in ("id", "kind", "source", "subject", "title", "tags", "collected_at")}
                for row in mem.latest_documents(conn, source="project", include_stale=True, limit=10)
            ]
    except Exception as exc:
        pack["context_store"]["error"] = f"{type(exc).__name__}: {exc}"
    try:
        status = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
        diff = subprocess.run(
            ["git", "diff", "--stat", "--", *[str(path) for path in manifest.get("write_scope", [])]],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
        pack["repository"] = {
            "git_status": status.stdout,
            "git_status_stderr": status.stderr,
            "write_scope_diff_stat": diff.stdout,
            "write_scope_diff_stderr": diff.stderr,
        }
    except Exception as exc:
        pack["repository"]["error"] = f"{type(exc).__name__}: {exc}"
    _write_json(context_pack_path(branch_id), pack)
    log_event({"event": "context_pack_built", "branch_id": branch_id})
    return pack


def create_branch(
    *,
    title: str,
    goal: str,
    write_scope: list[str] | None = None,
    read_scope: list[str] | None = None,
    verification: list[str] | None = None,
    dependencies: list[str] | None = None,
    context_topics: list[str] | None = None,
    agent_type: str = "worker",
) -> dict[str, Any]:
    branch_id = f"{int(now())}-{_slug(title)}-{uuid.uuid4().hex[:8]}"
    manifest = {
        "branch_id": branch_id,
        "title": title,
        "goal": goal,
        "agent_type": agent_type,
        "status": "planned",
        "created_at": iso(),
        "updated_at": iso(),
        "workspace": str(ROOT),
        "write_scope": write_scope or [],
        "read_scope": read_scope or [],
        "verification": verification or [],
        "dependencies": dependencies or [],
        "context_topics": context_topics or [],
        "context_tools": DEFAULT_CONTEXT_TOOLS,
        "mcp_access_rules": MCP_ACCESS_RULES,
        "agent_id": None,
        "notes": [],
    }
    _write_json(manifest_path(branch_id), manifest)
    log_event({"event": "branch_created", "branch_id": branch_id, "title": title})
    return manifest


def update_branch(branch_id: str, **updates: Any) -> dict[str, Any]:
    path = manifest_path(branch_id)
    if not path.exists():
        raise KeyError(f"unknown agent branch: {branch_id}")
    manifest = _read_json(path)
    manifest.update({key: value for key, value in updates.items() if value is not None})
    manifest["updated_at"] = iso()
    _write_json(path, manifest)
    log_event({"event": "branch_updated", "branch_id": branch_id, "updates": sorted(updates)})
    return manifest


def record_result(
    *,
    branch_id: str,
    status: str,
    summary: str,
    changed_files: list[str] | None = None,
    verification: list[str] | None = None,
    artifacts: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    manifest = update_branch(branch_id, status=status)
    result = {
        "branch_id": branch_id,
        "title": manifest.get("title"),
        "status": status,
        "summary": summary,
        "changed_files": changed_files or [],
        "verification": verification or [],
        "artifacts": artifacts or [],
        "notes": notes or [],
        "recorded_at": iso(),
    }
    _write_json(result_path(branch_id), result)
    log_event({"event": "branch_result", "branch_id": branch_id, "status": status})
    return result


def list_branches(limit: int = 50) -> dict[str, Any]:
    if not BRANCH_DIR.exists():
        return {"branches": []}
    rows = []
    for path in BRANCH_DIR.glob("*/manifest.json"):
        try:
            rows.append(_read_json(path))
        except Exception:
            continue
    rows.sort(key=lambda row: row.get("updated_at", ""), reverse=True)
    return {"branches": rows[: max(1, min(int(limit), 200))]}


def branch_status(branch_id: str) -> dict[str, Any]:
    manifest = _read_json(manifest_path(branch_id))
    result = _read_json(result_path(branch_id)) if result_path(branch_id).exists() else None
    return {"manifest": manifest, "result": result}


def build_agent_prompt(branch_id: str) -> dict[str, Any]:
    manifest = _read_json(manifest_path(branch_id))
    pack = build_context_pack(branch_id)
    pack_preview = json.dumps(_compact(pack, max_chars=16000), ensure_ascii=False, indent=2, default=str)
    lines = [
        f"Project workspace: {manifest['workspace']}",
        f"Branch id: {manifest['branch_id']}",
        f"Task title: {manifest['title']}",
        f"Context pack file: {context_pack_path(branch_id)}",
        "",
        "Goal:",
        manifest["goal"],
        "",
        "Context capsule:",
        "```json",
        pack_preview,
        "```",
        "",
        "Write scope:",
        *(f"- {item}" for item in manifest.get("write_scope", []) or ["No file edits; report only."]),
        "",
        "Read scope:",
        *(f"- {item}" for item in manifest.get("read_scope", []) or ["Use the files and MCP context needed for the task."]),
        "",
        "Required orientation:",
        *(f"- {tool}" for tool in manifest.get("context_tools", DEFAULT_CONTEXT_TOOLS)),
        "",
        "Rules:",
        *(f"- {rule}" for rule in manifest.get("mcp_access_rules", MCP_ACCESS_RULES)),
        "",
        "Verification expected:",
        *(f"- {item}" for item in manifest.get("verification", []) or ["Run the narrowest useful checks for your scope."]),
        "",
        "Final response contract:",
        "- Say whether you edited files.",
        "- List every changed path.",
        "- List verification commands/results.",
        "- Call out integration risks or conflicts.",
        "- Do not mark the overall user task complete; the parent agent integrates branches.",
    ]
    return {
        "branch_id": branch_id,
        "agent_type": manifest.get("agent_type", "worker"),
        "message": "\n".join(lines),
    }


def plan_branches(goal: str, branches: list[dict[str, Any]], include_prompts: bool = False) -> dict[str, Any]:
    if not branches:
        raise ValueError("branches cannot be empty")
    manifests = []
    for branch in branches:
        manifests.append(
            create_branch(
                title=str(branch["title"]),
                goal=str(branch.get("goal") or goal),
                write_scope=[str(item) for item in branch.get("write_scope", [])],
                read_scope=[str(item) for item in branch.get("read_scope", [])],
                verification=[str(item) for item in branch.get("verification", [])],
                dependencies=[str(item) for item in branch.get("dependencies", [])],
                context_topics=[str(item) for item in branch.get("context_topics", [])],
                agent_type=str(branch.get("agent_type") or "worker"),
            )
        )
    result = {
        "goal": goal,
        "branches": manifests,
        "prompts_included": include_prompts,
    }
    if include_prompts:
        result["prompts"] = [build_agent_prompt(row["branch_id"]) for row in manifests]
    else:
        result["next_step"] = "Call agent_branch_prompt(branch_id) for only the branches you are ready to spawn."
    return result


def integration_summary() -> dict[str, Any]:
    branches = list_branches(limit=200)["branches"]
    results = []
    for branch in branches:
        path = result_path(branch["branch_id"])
        if path.exists():
            results.append(_read_json(path))
    open_branches = [
        branch
        for branch in branches
        if str(branch.get("status") or "").strip().lower() not in TERMINAL_BRANCH_STATUSES
    ]
    return {
        "branch_count": len(branches),
        "result_count": len(results),
        "open": open_branches,
        "results": results,
    }
