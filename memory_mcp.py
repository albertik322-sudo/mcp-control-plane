from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import threading
import time
from typing import Any

import codex_memory as mem
from mcp_runtime import configure_stdio, create_mcp_server


configure_stdio()
mcp = create_mcp_server("codex-context-store")
DEFAULT_SOURCES = ("project", "tools", "windows", "server", "docker", "mikrotik", "homeassistant", "homelab")
_REFRESH_LOCK = threading.Lock()
_REFRESH_STATE_LOCK = threading.Lock()
_ACTIVE_REFRESH: dict[str, Any] | None = None


def _fresh_source_names(selected: list[str], ttl_by_source: dict[str, int]) -> set[str]:
    """Return sources whose latest successful collector run is inside its TTL."""
    if not selected:
        return set()
    placeholders = ",".join("?" for _ in selected)
    with mem.connect() as conn:
        rows = conn.execute(
            f"""
            select source, max(finished_at) as finished_at
            from collector_runs
            where status='ok' and source in ({placeholders})
            group by source
            """,
            selected,
        ).fetchall()
    now = time.time()
    return {
        str(row["source"])
        for row in rows
        if now - float(row["finished_at"]) < int(ttl_by_source.get(str(row["source"]), 0))
    }


def _compact_docs(rows: list[dict[str, Any]], max_body_chars: int = 1800) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if "body" in item and isinstance(item["body"], str) and len(item["body"]) > max_body_chars:
            item["body"] = item["body"][:max_body_chars] + "\n...[truncated]"
        out.append(item)
    return out


@mcp.tool()
def context_overview(topic: str | None = None, include_stale: bool = False, limit: int = 30) -> dict[str, Any]:
    """Return a compact Codex working context. Pass a topic to retrieve relevant stored data."""
    with mem.connect() as conn:
        fresh = mem.freshness(conn)
        core_facts = mem.facts(conn, include_stale=include_stale, limit=80)
        if topic:
            matches = mem.search(conn, topic, limit=max(5, limit))
            docs = []
        else:
            matches = []
            docs = mem.latest_documents(conn, include_stale=include_stale, limit=max(5, limit))
        runbooks = conn.execute("select title, body, tags, updated_at from runbooks order by updated_at desc").fetchall()
        return {
            "database": str(mem.db_path()),
            "topic": topic,
            "freshness": fresh,
            "facts": core_facts,
            "matches": matches,
            "latest_documents": _compact_docs(docs),
            "runbooks": [mem.row_to_dict(row) for row in runbooks],
        }


@mcp.tool()
def search_memory(query: str, limit: int = 20) -> dict[str, Any]:
    """Search the Codex context store with SQLite FTS."""
    with mem.connect() as conn:
        return {"query": query, "results": mem.search(conn, query, max(1, min(int(limit), 100)))}


@mcp.tool()
def get_source_context(source: str, include_stale: bool = False, limit: int = 30) -> dict[str, Any]:
    """Return latest documents for one source, including Change Memory source codex."""
    with mem.connect() as conn:
        return {
            "source": source,
            "documents": _compact_docs(mem.latest_documents(conn, source=source, include_stale=include_stale, limit=max(1, min(int(limit), 200))), 5000),
        }


@mcp.tool()
def get_document(document_id: int) -> dict[str, Any]:
    """Return a full stored document by id."""
    with mem.connect() as conn:
        row = conn.execute("select * from documents where id=?", (int(document_id),)).fetchone()
        if not row:
            return {"found": False, "id": document_id}
        return {"found": True, "document": mem.row_to_dict(row)}


@mcp.tool()
def list_sources() -> dict[str, Any]:
    """List stored sources and freshness information."""
    with mem.connect() as conn:
        return {"sources": mem.freshness(conn)}


@mcp.tool()
def stale_items() -> dict[str, Any]:
    """Return expired documents/facts."""
    with mem.connect() as conn:
        ts = mem.now_ts()
        docs = conn.execute(
            "select id, source, subject, title, collected_at, expires_at from documents where expires_at is not null and expires_at <= ? order by expires_at",
            (ts,),
        ).fetchall()
        facts = conn.execute(
            "select id, namespace, key, source, updated_at, expires_at from facts where expires_at is not null and expires_at <= ? order by expires_at",
            (ts,),
        ).fetchall()
        return {"documents": [mem.row_to_dict(row) for row in docs], "facts": [mem.row_to_dict(row) for row in facts]}


@mcp.tool()
def remember_fact(namespace: str, key: str, value: Any, tags: list[str] | None = None, ttl_seconds: int | None = None) -> dict[str, Any]:
    """Store or update a durable fact for future tasks."""
    with mem.connect() as conn:
        fact_id = mem.upsert_fact(conn, namespace=namespace, key=key, value=value, source="manual", tags=tags, ttl_seconds=ttl_seconds)
        conn.commit()
        return {"id": fact_id, "namespace": namespace, "key": key}


@mcp.tool()
def add_runbook(title: str, body: str, tags: list[str] | None = None) -> dict[str, Any]:
    """Add or update a runbook note."""
    with mem.connect() as conn:
        cur = conn.execute(
            """
            insert into runbooks(title, body, tags, updated_at)
            values(?,?,?,?)
            on conflict(title) do update set body=excluded.body, tags=excluded.tags, updated_at=excluded.updated_at
            returning id
            """,
            (title, body, mem.tags_text(tags), mem.now_ts()),
        )
        runbook_id = int(cur.fetchone()["id"])
        mem.refresh_runbook_fts(conn, runbook_id, title, body, mem.tags_text(tags))
        conn.commit()
        return {"id": runbook_id, "title": title, "tags": tags or []}


@mcp.tool()
def record_change(
    title: str,
    summary: str,
    areas: list[str] | None = None,
    files: list[str] | None = None,
    commands: list[str] | None = None,
    verification: list[str] | None = None,
    refresh_sources: list[str] | None = None,
    tags: list[str] | None = None,
    status: str = "completed",
    risk: str | None = None,
    rollback: str | None = None,
) -> dict[str, Any]:
    """Record a durable change event after code, config, or infrastructure changes."""
    if not title.strip() or not summary.strip():
        raise ValueError("title and summary must not be empty")
    with mem.connect() as conn:
        doc_id = mem.record_change(
            conn,
            title=title,
            summary=summary,
            areas=areas,
            files=files,
            commands=commands,
            verification=verification,
            refresh_sources=refresh_sources,
            tags=tags,
            status=status,
            risk=risk,
            rollback=rollback,
        )
        conn.commit()
        return {"id": doc_id, "title": title, "refresh_sources": refresh_sources or []}


@mcp.tool()
def list_changes(limit: int = 20, area: str | None = None) -> dict[str, Any]:
    """List recent durable change events, optionally filtered by area/tag."""
    with mem.connect() as conn:
        return {"changes": mem.list_changes(conn, limit=max(1, min(int(limit), 200)), area=area)}


@mcp.tool()
def change_summary(limit: int = 10) -> dict[str, Any]:
    """Return compact Change Memory totals and recent events."""
    with mem.connect() as conn:
        return mem.change_summary(conn, limit=max(1, min(int(limit), 50)))


@mcp.tool()
def rebuild_search_index() -> dict[str, Any]:
    """Rebuild Context Store FTS index for documents, facts, and runbooks."""
    with mem.connect() as conn:
        mem.rebuild_fts(conn)
        conn.commit()
        return {"ok": True}


@mcp.tool()
def refresh_context(
    sources: list[str] | None = None,
    publish_observability: bool | None = None,
    skip_fresh: bool | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Refresh Context Store sources without stacking duplicate work.

    Full refreshes skip already-fresh sources by default. Targeted refreshes
    force their requested sources by default. Set skip_fresh explicitly to
    override either behavior.
    """
    global _ACTIVE_REFRESH
    started = time.time()
    import context_collector

    source_args = list(dict.fromkeys(str(source).strip().lower() for source in (sources or []) if str(source).strip()))
    selected = source_args or list(DEFAULT_SOURCES)
    supported_sources = set(context_collector.COLLECTORS) | set(context_collector.MANAGED_SOURCES)
    unknown_sources = [source for source in selected if source not in supported_sources]
    if unknown_sources:
        return {
            "result": {"sources": {}},
            "duration_seconds": round(time.time() - started, 3),
            "exit_code": 1,
            "mode": "invalid-request",
            "unknown_sources": unknown_sources,
            "supported_sources": sorted(supported_sources),
        }

    should_publish = (not source_args) if publish_observability is None else bool(publish_observability)
    should_skip_fresh = (not source_args) if skip_fresh is None else bool(skip_fresh)
    workers = max(1, min(int(max_workers), 8, len(selected)))

    if not _REFRESH_LOCK.acquire(blocking=False):
        with _REFRESH_STATE_LOCK:
            active = dict(_ACTIVE_REFRESH or {})
        return {
            "result": {"sources": {}, "codex_observability": {"ok": True, "skipped": True, "reason": "refresh already active"}},
            "duration_seconds": round(time.time() - started, 3),
            "exit_code": 2,
            "mode": "busy",
            "active_refresh": active,
        }

    with _REFRESH_STATE_LOCK:
        _ACTIVE_REFRESH = {
            "started_at": started,
            "sources": selected,
            "skip_fresh": should_skip_fresh,
            "max_workers": workers,
        }

    try:
        result: dict[str, Any] = {"sources": {}, "codex_observability": None}

        fresh_sources: set[str] = set()
        if should_skip_fresh:
            collectable_selected = [source for source in selected if source in context_collector.COLLECTORS]
            fresh_sources = _fresh_source_names(collectable_selected, context_collector.DEFAULT_TTL)

        managed_sources = [source for source in selected if source in context_collector.MANAGED_SOURCES]
        pending = [
            source
            for source in selected
            if source not in fresh_sources and source not in context_collector.MANAGED_SOURCES
        ]
        source_results: dict[str, dict[str, Any]] = {
            source: {"ok": True, "skipped": True, "reason": "source is fresh", "duration_seconds": 0.0}
            for source in selected
            if source in fresh_sources
        }
        source_results.update(
            {
                source: {
                    "ok": True,
                    "skipped": True,
                    "managed": True,
                    "reason": context_collector.MANAGED_SOURCES[source],
                    "duration_seconds": 0.0,
                }
                for source in managed_sources
            }
        )

        def collect_one(source: str) -> dict[str, Any]:
            source_started = time.time()
            try:
                source_result = context_collector.collect_selected([source], publish=False)
                return {
                    "ok": bool(source_result.get(source, {}).get("ok")),
                    "result": source_result,
                    "duration_seconds": round(time.time() - source_started, 3),
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "type": type(exc).__name__,
                    "duration_seconds": round(time.time() - source_started, 3),
                }

        if pending:
            with ThreadPoolExecutor(max_workers=min(workers, len(pending)), thread_name_prefix="context-refresh") as pool:
                futures = {pool.submit(collect_one, source): source for source in pending}
                for future in as_completed(futures):
                    source_results[futures[future]] = future.result()
        result["sources"] = {source: source_results[source] for source in selected}

        if should_publish:
            observability_started = time.time()
            try:
                result["codex_observability"] = {
                    "ok": True,
                    "result": {"codex_observability": context_collector.publish_observability()},
                    "duration_seconds": round(time.time() - observability_started, 3),
                }
            except Exception as exc:
                result["codex_observability"] = {
                    "ok": False,
                    "error": str(exc),
                    "type": type(exc).__name__,
                    "duration_seconds": round(time.time() - observability_started, 3),
                }
        else:
            result["codex_observability"] = {"ok": True, "skipped": True, "reason": "publishing disabled"}
        return {
            "result": result,
            "duration_seconds": round(time.time() - started, 3),
            "exit_code": 0 if all(item.get("ok") for item in result["sources"].values()) else 1,
            "mode": "parallel-in-process",
            "max_workers": workers,
            "refreshed_sources": pending,
            "skipped_sources": [
                source
                for source in selected
                if source in fresh_sources or source in context_collector.MANAGED_SOURCES
            ],
        }
    finally:
        with _REFRESH_STATE_LOCK:
            _ACTIVE_REFRESH = None
        _REFRESH_LOCK.release()


@mcp.tool()
def database_stats() -> dict[str, Any]:
    """Return SQLite context store table counts and database path."""
    with mem.connect() as conn:
        tables = ["facts", "documents", "collector_runs", "incidents", "runbooks"]
        counts = {table: conn.execute(f"select count(*) from {table}").fetchone()[0] for table in tables}
        counts["change_documents"] = conn.execute("select count(*) from documents where kind='change' and source='codex'").fetchone()[0]
        try:
            counts["memory_fts"] = conn.execute("select count(*) from memory_fts").fetchone()[0]
        except Exception:
            counts["memory_fts"] = None
        recent_runs = conn.execute(
            "select source, status, started_at, finished_at, duration_seconds, message from collector_runs order by id desc limit 20"
        ).fetchall()
        return {
            "database": str(mem.db_path()),
            "counts": counts,
            "recent_changes": mem.list_changes(conn, limit=5),
            "recent_runs": [mem.row_to_dict(row) for row in recent_runs],
        }


if __name__ == "__main__":
    mcp.run()
