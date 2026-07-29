from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import codex_memory as mem


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
STATUS_JSON = RUNTIME / "mcp_observability_status.json"
STATUS_PROM = RUNTIME / "mcp_observability.prom"
REMOTE_JSON = "/var/lib/homelab-observability/codex_mcp_status.json"
REMOTE_PROM = "/var/lib/node_exporter/textfile_collector/codex_mcp.prom"


def _age_seconds(ts: float | None) -> int | None:
    if not ts:
        return None
    return max(0, int(time.time() - float(ts)))


def _metric_name(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.lower()).strip("_")


def build_status() -> dict[str, Any]:
    with mem.connect() as conn:
        freshness = mem.freshness(conn)
        fresh = 0
        stale = 0
        newest = None
        sources: dict[str, Any] = {}
        now = mem.now_ts()
        for row in freshness:
            row = dict(row)
            latest = row.get("latest")
            newest = max(newest or 0, latest or 0)
            is_stale = bool(row.get("stale"))
            stale += 1 if is_stale else 0
            fresh += 0 if is_stale else 1
            sources[row["source"]] = {
                "documents": row.get("documents", 0),
                "stale_documents": row.get("stale", 0),
                "latest": latest,
                "age_seconds": _age_seconds(latest),
            }
        runs_24h = conn.execute(
            """
            select status, count(*) as count, max(finished_at) as latest, avg(duration_seconds) as avg_duration
            from collector_runs
            where started_at >= ?
            group by status
            """,
            (now - 86400,),
        ).fetchall()
        collector_counts = {row["status"]: int(row["count"]) for row in runs_24h}
        latest_change = mem.list_changes(conn, limit=1)
        doc_drift = conn.execute(
            "select payload_json, collected_at, status, error from documents where source='homelab' and subject='doc-drift' order by collected_at desc limit 1"
        ).fetchone()
        drift_payload: dict[str, Any] = {}
        if doc_drift and doc_drift["payload_json"]:
            try:
                drift_payload = json.loads(doc_drift["payload_json"])
            except Exception:
                drift_payload = {"parse_error": True}
        change_total = conn.execute("select count(*) from documents where kind='change' and source='codex'").fetchone()[0]
        db_size = mem.db_path().stat().st_size if mem.db_path().exists() else 0

    errors_24h = collector_counts.get("error", 0)
    status = "ok"
    if stale:
        status = "stale"
    if errors_24h:
        status = "error"

    return {
        "status": status,
        "generated_at": mem.iso(),
        "generated_at_epoch": int(time.time()),
        "database": str(mem.db_path()),
        "database_bytes": db_size,
        "sources": {
            "fresh": fresh,
            "stale": stale,
            "total": len(freshness),
            "latest_age_seconds": _age_seconds(newest),
            "items": sources,
        },
        "collectors": {
            "ok_24h": collector_counts.get("ok", 0),
            "errors_24h": errors_24h,
            "latest_finished_age_seconds": _age_seconds(max((row["latest"] for row in runs_24h if row["latest"]), default=None)),
        },
        "changes": {
            "total": change_total,
            "latest_title": latest_change[0]["title"] if latest_change else "",
            "latest_age_seconds": _age_seconds(latest_change[0]["collected_at"]) if latest_change else None,
        },
        "doc_drift": {
            "status": drift_payload.get("status") or ("unknown" if not drift_payload else "ok"),
            "finding_count": int(drift_payload.get("finding_count") or 0),
            "warnings": int(drift_payload.get("severities", {}).get("warning", 0)) if isinstance(drift_payload.get("severities"), dict) else 0,
            "age_seconds": _age_seconds(doc_drift["collected_at"]) if doc_drift else None,
        },
    }


def render_prometheus(status: dict[str, Any]) -> str:
    lines = [
        "# HELP codex_mcp_context_sources Number of Context Store sources by state.",
        "# TYPE codex_mcp_context_sources gauge",
        f"codex_mcp_context_sources{{state=\"fresh\"}} {status['sources']['fresh']}",
        f"codex_mcp_context_sources{{state=\"stale\"}} {status['sources']['stale']}",
        "# HELP codex_mcp_collector_runs_24h Context collector runs in the last 24 hours.",
        "# TYPE codex_mcp_collector_runs_24h gauge",
        f"codex_mcp_collector_runs_24h{{status=\"ok\"}} {status['collectors']['ok_24h']}",
        f"codex_mcp_collector_runs_24h{{status=\"error\"}} {status['collectors']['errors_24h']}",
        "# HELP codex_mcp_change_events_total Durable Change Memory event count.",
        "# TYPE codex_mcp_change_events_total gauge",
        f"codex_mcp_change_events_total {status['changes']['total']}",
        "# HELP codex_mcp_doc_drift_findings Documentation drift findings.",
        "# TYPE codex_mcp_doc_drift_findings gauge",
        f"codex_mcp_doc_drift_findings {status['doc_drift']['finding_count']}",
        "# HELP codex_mcp_observability_last_push_timestamp_seconds Last observability snapshot generation.",
        "# TYPE codex_mcp_observability_last_push_timestamp_seconds gauge",
        f"codex_mcp_observability_last_push_timestamp_seconds {status['generated_at_epoch']}",
    ]
    for source, item in status["sources"]["items"].items():
        name = _metric_name(source)
        age = item.get("age_seconds")
        if age is not None:
            lines.append(f'codex_mcp_source_age_seconds{{source="{name}"}} {age}')
    return "\n".join(lines) + "\n"


def write_status_files() -> dict[str, Any]:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    status = build_status()
    STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    STATUS_PROM.write_text(render_prometheus(status), encoding="utf-8")
    return status


def publish_status_to_server(status: dict[str, Any] | None = None) -> dict[str, Any]:
    import server

    status = status or build_status()
    server._exec("install -d -m 0755 /var/lib/homelab-observability /var/lib/node_exporter/textfile_collector", sudo=True, timeout=20)
    json_result = server.write_file(REMOTE_JSON, json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), sudo=True, mode="0644")
    prom_result = server.write_file(REMOTE_PROM, render_prometheus(status), sudo=True, mode="0644")
    return {"json": json_result, "prometheus": prom_result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and optionally publish Codex MCP observability snapshot")
    parser.add_argument("--publish", action="store_true", help="Publish JSON and Prometheus textfile metrics to the Ubuntu server")
    args = parser.parse_args()
    status = write_status_files()
    result: dict[str, Any] = {"local": {"json": str(STATUS_JSON), "prometheus": str(STATUS_PROM)}, "status": status}
    if args.publish:
        result["publish"] = publish_status_to_server(status)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
