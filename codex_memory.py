from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
DB_PATH = RUNTIME / "codex_context.sqlite"
_INIT_LOCK = threading.Lock()
_INITIALIZED_DATABASES: set[str] = set()

SECRET_KEY_RE = re.compile(r"(password|passwd|passphrase|token|secret|api[_-]?key|authorization|bearer|cookie|credential)", re.I)
SECRET_VALUE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]{16,}|sk_live_[a-z0-9_-]{16,}|eyJ[a-z0-9_-]{20,}\.[a-z0-9_-]{20,}\.[a-z0-9_-]{20,}|Bearer\s+[a-z0-9._-]{16,})"
)


def now_ts() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now_ts()))


def db_path() -> Path:
    RUNTIME.mkdir(exist_ok=True)
    return DB_PATH


def connect() -> sqlite3.Connection:
    path = db_path()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=30000")
    path_key = str(path.resolve())
    if path_key not in _INITIALIZED_DATABASES:
        with _INIT_LOCK:
            if path_key not in _INITIALIZED_DATABASES:
                init_db(conn)
                _INITIALIZED_DATABASES.add(path_key)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        pragma journal_mode=wal;
        create table if not exists facts (
            id integer primary key,
            namespace text not null,
            key text not null,
            value_text text not null,
            payload_json text,
            source text not null,
            confidence real not null default 1.0,
            tags text not null default '',
            created_at real not null,
            updated_at real not null,
            expires_at real,
            unique(namespace, key)
        );
        create table if not exists documents (
            id integer primary key,
            kind text not null,
            source text not null,
            subject text not null,
            title text not null,
            body text not null,
            payload_json text,
            tags text not null default '',
            status text not null default 'ok',
            error text,
            checksum text not null,
            collected_at real not null,
            expires_at real,
            unique(kind, source, subject)
        );
        create table if not exists collector_runs (
            id integer primary key,
            source text not null,
            status text not null,
            started_at real not null,
            finished_at real not null,
            duration_seconds real not null,
            message text
        );
        create table if not exists incidents (
            id integer primary key,
            title text not null,
            status text not null default 'open',
            severity text not null default 'info',
            summary text not null,
            details_json text,
            tags text not null default '',
            created_at real not null,
            updated_at real not null
        );
        create table if not exists runbooks (
            id integer primary key,
            title text not null unique,
            body text not null,
            tags text not null default '',
            updated_at real not null
        );
        create index if not exists idx_documents_source_kind_collected on documents(source, kind, collected_at);
        create index if not exists idx_documents_expires on documents(expires_at);
        create index if not exists idx_documents_kind_collected on documents(kind, collected_at);
        create index if not exists idx_facts_namespace_updated on facts(namespace, updated_at);
        create index if not exists idx_facts_expires on facts(expires_at);
        create index if not exists idx_collector_runs_source_started on collector_runs(source, started_at);
        """
    )
    try:
        row = conn.execute("select sql from sqlite_master where type='table' and name='memory_fts'").fetchone()
        if row and "content=''" in (row["sql"] or ""):
            conn.execute("drop table memory_fts")
        conn.execute("create virtual table if not exists memory_fts using fts5(kind, source, subject, title, body, tags)")
    except sqlite3.OperationalError:
        pass
    seed_runbooks(conn)
    try:
        fts_count = conn.execute("select count(*) from memory_fts").fetchone()[0]
        if not fts_count:
            rebuild_fts(conn)
    except sqlite3.OperationalError:
        pass
    conn.commit()


def seed_runbooks(conn: sqlite3.Connection) -> None:
    rows = {
        "Ubuntu server basics": (
            "SSH target is configured through REMOTE_* in .env. Use sudo for service, UFW, Docker, Apache, Nextcloud and observability stack changes. Prefer backups before editing configs.",
            "server,ssh,ubuntu,runbook",
        ),
        "Homelab observability": (
            "Example local URLs: home.example.lan, grafana.example.lan, status.example.lan, prometheus.example.lan, ha.example.lan. Configure real endpoints through environment variables.",
            "homelab,prometheus,grafana,uptime-kuma,homepage",
        ),
        "MikroTik notes": (
            "MikroTik hAP ax3 is managed via RouterOS API/SSH at MIKROTIK_HOST. DNS DoH was disabled after dns.google DoH timeouts; DHCP lease-time was increased to 12h.",
            "mikrotik,router,wifi,dns",
        ),
    }
    ts = now_ts()
    for title, (body, tags) in rows.items():
        conn.execute(
            """
            insert into runbooks(title, body, tags, updated_at)
            values(?,?,?,?)
            on conflict(title) do nothing
            """,
            (title, body, tags, ts),
        )


def rebuild_fts(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("delete from memory_fts")
        docs = conn.execute("select id, kind, source, subject, title, body, tags from documents").fetchall()
        for row in docs:
            conn.execute(
                "insert into memory_fts(rowid, kind, source, subject, title, body, tags) values(?,?,?,?,?,?,?)",
                (fts_rowid("document", row["id"]), row["kind"], row["source"], row["subject"], row["title"], row["body"], row["tags"]),
            )
        fact_rows = conn.execute("select id, namespace, key, value_text, source, tags from facts").fetchall()
        for row in fact_rows:
            conn.execute(
                "insert into memory_fts(rowid, kind, source, subject, title, body, tags) values(?,?,?,?,?,?,?)",
                (
                    fts_rowid("fact", row["id"]),
                    row["namespace"],
                    row["source"],
                    row["key"],
                    f"{row['namespace']}.{row['key']}",
                    row["value_text"],
                    row["tags"],
                ),
            )
        runbook_rows = conn.execute("select id, title, body, tags from runbooks").fetchall()
        for row in runbook_rows:
            conn.execute(
                "insert into memory_fts(rowid, kind, source, subject, title, body, tags) values(?,?,?,?,?,?,?)",
                (
                    fts_rowid("runbook", row["id"]),
                    "runbook",
                    "manual",
                    row["title"],
                    row["title"],
                    row["body"],
                    row["tags"],
                ),
            )
    except sqlite3.OperationalError:
        return


def sanitize(value: Any, key_hint: str = "") -> Any:
    if SECRET_KEY_RE.search(key_hint):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, key_hint) for v in value]
    if isinstance(value, tuple):
        return [sanitize(v, key_hint) for v in value]
    if isinstance(value, str):
        text = SECRET_VALUE_RE.sub("[redacted]", value)
        # Redact common key=value and YAML-ish secret fields inside command output.
        text = re.sub(
            r"(?im)^(\s*[^=\n:]{0,40}(password|passwd|passphrase|token|secret|api[_-]?key|authorization)[^=\n:]{0,20}\s*[:=]\s*).+$",
            r"\1[redacted]",
            text,
        )
        return text
    return value


def to_json(value: Any) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False, default=str, sort_keys=True)


def to_text(value: Any, max_chars: int = 200000) -> str:
    value = sanitize(value)
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str, indent=2, sort_keys=True)
    return text[:max_chars]


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def tags_text(tags: list[str] | tuple[str, ...] | str | None) -> str:
    if tags is None:
        return ""
    if isinstance(tags, str):
        return tags
    return ",".join(str(tag).strip() for tag in tags if str(tag).strip())


def upsert_document(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source: str,
    subject: str,
    title: str,
    payload: Any,
    body: str | None = None,
    tags: list[str] | str | None = None,
    ttl_seconds: int | None = None,
    status: str = "ok",
    error: str | None = None,
) -> int:
    ts = now_ts()
    body_text = to_text(body if body is not None else payload)
    payload_json = to_json(payload)
    expires_at = ts + ttl_seconds if ttl_seconds else None
    digest = checksum(body_text + payload_json)
    tag_s = tags_text(tags)
    cur = conn.execute(
        """
        insert into documents(kind, source, subject, title, body, payload_json, tags, status, error, checksum, collected_at, expires_at)
        values(?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(kind, source, subject) do update set
            title=excluded.title,
            body=excluded.body,
            payload_json=excluded.payload_json,
            tags=excluded.tags,
            status=excluded.status,
            error=excluded.error,
            checksum=excluded.checksum,
            collected_at=excluded.collected_at,
            expires_at=excluded.expires_at
        returning id
        """,
        (kind, source, subject, title, body_text, payload_json, tag_s, status, error, digest, ts, expires_at),
    )
    doc_id = int(cur.fetchone()["id"])
    refresh_fts(conn, "document", doc_id, kind, source, subject, title, body_text, tag_s)
    return doc_id


def upsert_fact(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    key: str,
    value: Any,
    source: str = "manual",
    confidence: float = 1.0,
    tags: list[str] | str | None = None,
    ttl_seconds: int | None = None,
) -> int:
    ts = now_ts()
    value_text = to_text(value, max_chars=20000)
    payload_json = to_json(value)
    expires_at = ts + ttl_seconds if ttl_seconds else None
    tag_s = tags_text(tags)
    cur = conn.execute(
        """
        insert into facts(namespace, key, value_text, payload_json, source, confidence, tags, created_at, updated_at, expires_at)
        values(?,?,?,?,?,?,?,?,?,?)
        on conflict(namespace, key) do update set
            value_text=excluded.value_text,
            payload_json=excluded.payload_json,
            source=excluded.source,
            confidence=excluded.confidence,
            tags=excluded.tags,
            updated_at=excluded.updated_at,
            expires_at=excluded.expires_at
        returning id
        """,
        (namespace, key, value_text, payload_json, source, confidence, tag_s, ts, ts, expires_at),
    )
    fact_id = int(cur.fetchone()["id"])
    refresh_fts(conn, "fact", fact_id, namespace, source, key, f"{namespace}.{key}", value_text, tag_s)
    return fact_id


def record_change(
    conn: sqlite3.Connection,
    *,
    title: str,
    summary: str,
    areas: list[str] | str | None = None,
    files: list[str] | None = None,
    commands: list[str] | None = None,
    verification: list[str] | None = None,
    refresh_sources: list[str] | None = None,
    tags: list[str] | str | None = None,
    status: str = "completed",
    risk: str | None = None,
    rollback: str | None = None,
) -> int:
    change_id = uuid.uuid4().hex
    payload = {
        "change_id": change_id,
        "title": title,
        "summary": summary,
        "status": status,
        "risk": risk or "",
        "rollback": rollback or "",
        "areas": areas if isinstance(areas, list) else ([areas] if areas else []),
        "files": files or [],
        "commands": commands or [],
        "verification": verification or [],
        "refresh_sources": refresh_sources or [],
        "recorded_at": iso(),
    }
    tag_values = ["change"]
    if tags:
        tag_values.extend(tags if isinstance(tags, list) else [tags])
    if refresh_sources:
        tag_values.extend(refresh_sources)
    subject = re.sub(r"[^a-z0-9_.-]+", "-", title.lower()).strip("-")[:80] or f"change-{int(now_ts())}"
    return upsert_document(
        conn,
        kind="change",
        source="codex",
        subject=f"{int(now_ts())}-{subject}-{change_id[:8]}",
        title=title,
        payload=payload,
        tags=tag_values,
        ttl_seconds=None,
    )


def refresh_fts(
    conn: sqlite3.Connection,
    ref_type: str,
    ref_id: int,
    kind: str,
    source: str,
    subject: str,
    title: str,
    body: str,
    tags: str,
) -> None:
    try:
        rowid = fts_rowid(ref_type, ref_id)
        conn.execute("delete from memory_fts where rowid=?", (rowid,))
        conn.execute(
            "insert into memory_fts(rowid, kind, source, subject, title, body, tags) values(?,?,?,?,?,?,?)",
            (rowid, kind, source, subject, title, body, tags),
        )
    except sqlite3.OperationalError:
        return


def fts_rowid(ref_type: str, ref_id: int) -> int:
    # Keep document, fact, and runbook ids in separate rowid ranges.
    if ref_type == "document":
        return ref_id
    if ref_type == "fact":
        return 1_000_000_000 + ref_id
    if ref_type == "runbook":
        return 2_000_000_000 + ref_id
    raise ValueError(f"unknown FTS ref_type: {ref_type}")


def refresh_runbook_fts(conn: sqlite3.Connection, runbook_id: int, title: str, body: str, tags: str) -> None:
    refresh_fts(conn, "runbook", runbook_id, "runbook", "manual", title, title, body, tags)


def record_run(conn: sqlite3.Connection, source: str, status: str, started: float, message: str = "") -> None:
    finished = now_ts()
    conn.execute(
        "insert into collector_runs(source, status, started_at, finished_at, duration_seconds, message) values(?,?,?,?,?,?)",
        (source, status, started, finished, round(finished - started, 3), message[:4000]),
    )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("payload_json", "details_json"):
        if out.get(key):
            try:
                out[key.removesuffix("_json")] = json.loads(out[key])
            except Exception:
                pass
    return out


def _search_terms(query: str) -> list[str]:
    terms = re.findall(r"[\w./:@-]+", query, flags=re.UNICODE)
    clean: list[str] = []
    for term in terms:
        term = term.strip(".:/@-_").lower()
        if len(term) < 2:
            continue
        if term not in clean:
            clean.append(term)
    return clean[:16]


def _fts_or_query(query: str) -> str:
    parts = []
    for term in _search_terms(query):
        escaped = term.replace('"', '""')
        if re.fullmatch(r"[\w]+", escaped, flags=re.UNICODE):
            parts.append(f"{escaped}*")
        else:
            parts.append(f'"{escaped}"')
    return " OR ".join(parts)


def _like_search(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    like = f"%{query}%"
    rows = conn.execute(
        """
        select id as rowid, kind, source, subject, title, substr(body,1,700) as snippet, tags, collected_at as updated_at
        from documents
        where title like ? or body like ? or tags like ?
        union all
        select 1000000000 + id as rowid, namespace as kind, source, key as subject,
               namespace || '.' || key as title, substr(value_text,1,700) as snippet, tags, updated_at
        from facts
        where namespace like ? or key like ? or value_text like ? or tags like ?
        union all
        select 2000000000 + id as rowid, 'runbook' as kind, 'manual' as source, title as subject,
               title, substr(body,1,700) as snippet, tags, updated_at
        from runbooks
        where title like ? or body like ? or tags like ?
        order by updated_at desc
        limit ?
        """,
        (like, like, like, like, like, like, like, like, like, like, limit),
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return []
    fts_queries = [query]
    fallback = _fts_or_query(query)
    if fallback and fallback != query:
        fts_queries.append(fallback)
    for fts_query in fts_queries:
        try:
            rows = conn.execute(
                """
                select rowid, kind, source, subject, title, snippet(memory_fts, 4, '[', ']', ' ... ', 24) as snippet, tags,
                       bm25(memory_fts) as rank
                from memory_fts
                where memory_fts match ?
                order by rank
                limit ?
                """,
                (fts_query, limit),
            ).fetchall()
            if rows:
                return [row_to_dict(row) for row in rows]
        except sqlite3.OperationalError:
            continue
    return _like_search(conn, query, limit)


def list_changes(conn: sqlite3.Connection, limit: int = 20, area: str | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select id, title, subject, body, payload_json, tags, collected_at
        from documents
        where kind='change' and source='codex'
        order by collected_at desc
        limit ?
        """,
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_dict(row)
        payload = item.get("payload") or {}
        if area:
            areas = [str(value).lower() for value in payload.get("areas", [])]
            tags = str(item.get("tags", "")).lower()
            if area.lower() not in areas and area.lower() not in tags:
                continue
        out.append(
            {
                "id": item["id"],
                "title": item["title"],
                "subject": item["subject"],
                "collected_at": item["collected_at"],
                "tags": item.get("tags", ""),
                "payload": payload,
            }
        )
    return out


def change_summary(conn: sqlite3.Connection, limit: int = 10) -> dict[str, Any]:
    changes = list_changes(conn, limit=limit)
    by_area: dict[str, int] = {}
    for change in changes:
        for area in change.get("payload", {}).get("areas", []) or ["unspecified"]:
            by_area[str(area)] = by_area.get(str(area), 0) + 1
    total = conn.execute("select count(*) from documents where kind='change' and source='codex'").fetchone()[0]
    return {"total": total, "recent": changes, "recent_by_area": by_area}


def latest_documents(
    conn: sqlite3.Connection,
    source: str | None = None,
    kind: str | None = None,
    include_stale: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if source:
        clauses.append("source=?")
        params.append(source)
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if not include_stale:
        clauses.append("(expires_at is null or expires_at > ?)")
        params.append(now_ts())
    where = "where " + " and ".join(clauses) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        select id, kind, source, subject, title, body, tags, status, error, collected_at, expires_at
        from documents
        {where}
        order by collected_at desc
        limit ?
        """,
        params,
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def facts(
    conn: sqlite3.Connection,
    namespace: str | None = None,
    include_stale: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if namespace:
        clauses.append("namespace=?")
        params.append(namespace)
    if not include_stale:
        clauses.append("(expires_at is null or expires_at > ?)")
        params.append(now_ts())
    where = "where " + " and ".join(clauses) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        select id, namespace, key, value_text, source, confidence, tags, created_at, updated_at, expires_at
        from facts
        {where}
        order by updated_at desc
        limit ?
        """,
        params,
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def freshness(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select source, count(*) as documents, max(collected_at) as latest,
               sum(case when expires_at is not null and expires_at <= ? then 1 else 0 end) as stale
        from documents
        group by source
        order by latest desc
        """,
        (now_ts(),),
    ).fetchall()
    return [row_to_dict(row) for row in rows]
