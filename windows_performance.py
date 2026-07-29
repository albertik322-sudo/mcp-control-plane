from __future__ import annotations

import socket
import time
from typing import Any, Iterable

import psutil


DEFAULT_LATENCY_TARGETS = [
    {"name": "router", "host": "192.168.1.1", "port": 53},
    {"name": "ubuntu-server", "host": "192.168.1.10", "port": 22},
    {"name": "homepage", "host": "home.example.lan", "port": 80},
    {"name": "homeassistant", "host": "192.168.1.20", "port": 8123},
]


def _bounded_int(value: int | float | str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(parsed, maximum))


def _mb(value: int | float | None) -> float:
    return round(float(value or 0) / 1024**2, 1)


def _gb(value: int | float | None) -> float:
    return round(float(value or 0) / 1024**3, 2)


def _percent(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return round((float(part) / float(total)) * 100, 1)


def summarize_durations(rows: Iterable[dict[str, Any]], field: str = "duration_ms") -> dict[str, Any]:
    values = sorted(float(row[field]) for row in rows if row.get(field) is not None)
    if not values:
        return {"count": 0, "avg_ms": None, "min_ms": None, "max_ms": None, "p95_ms": None}
    p95_index = min(len(values) - 1, int(round((len(values) - 1) * 0.95)))
    return {
        "count": len(values),
        "avg_ms": round(sum(values) / len(values), 1),
        "min_ms": round(values[0], 1),
        "max_ms": round(values[-1], 1),
        "p95_ms": round(values[p95_index], 1),
    }


def score_process_group(row: dict[str, Any], total_memory_mb: float) -> dict[str, Any]:
    rss_mb = float(row.get("rss_mb") or 0)
    cpu_percent = float(row.get("cpu_percent") or 0)
    count = int(row.get("count") or 0)
    reasons: list[str] = []
    score = 0
    if total_memory_mb and rss_mb > total_memory_mb * 0.12:
        score += 3
        reasons.append("uses more than 12% of physical memory")
    elif total_memory_mb and rss_mb > total_memory_mb * 0.06:
        score += 2
        reasons.append("uses more than 6% of physical memory")
    if cpu_percent >= 25:
        score += 3
        reasons.append("high sampled CPU")
    elif cpu_percent >= 10:
        score += 2
        reasons.append("moderate sampled CPU")
    if count >= 15:
        score += 1
        reasons.append("many related processes")
    return {
        "name": row.get("name") or "unknown",
        "impact_score": score,
        "reasons": reasons,
        "approval_required": True,
        "safe_action": "review workload, tabs, extensions, or app settings before changing anything",
    }


def process_group_impact(processes: Iterable[dict[str, Any]], total_memory_mb: float, limit: int = 20) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for proc in processes:
        name = str(proc.get("name") or "unknown")
        row = groups.setdefault(
            name,
            {"name": name, "count": 0, "rss_mb": 0.0, "cpu_percent": 0.0, "threads": 0, "pids": []},
        )
        row["count"] += 1
        row["rss_mb"] += float(proc.get("rss_mb") or 0)
        row["cpu_percent"] += float(proc.get("cpu_percent") or 0)
        row["threads"] += int(proc.get("threads") or 0)
        if proc.get("pid") is not None:
            row["pids"].append(proc["pid"])
    rows = []
    for row in groups.values():
        row["rss_mb"] = round(row["rss_mb"], 1)
        row["cpu_percent"] = round(row["cpu_percent"], 1)
        scored = score_process_group(row, total_memory_mb)
        row.update(scored)
        rows.append(row)
    return sorted(rows, key=lambda item: (item["impact_score"], item["rss_mb"], item["cpu_percent"]), reverse=True)[:limit]


def startup_impact(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        command = str(item.get("Command") or item.get("command") or "")
        location = str(item.get("Location") or item.get("location") or "")
        score = 1
        reasons = ["starts during sign-in or boot"]
        lower = f"{command} {location}".lower()
        if "run" in lower or "startup" in lower:
            score += 1
        if any(token in lower for token in ("updater", "helper", "launcher", "daemon", "service")):
            score += 1
            reasons.append("background helper/updater pattern")
        rows.append(
            {
                "name": item.get("Name") or item.get("name") or "unknown",
                "location": location,
                "command": command[:500],
                "impact_score": score,
                "reasons": reasons,
                "approval_required": True,
                "safe_action": "verify it is still needed before disabling it",
            }
        )
    return sorted(rows, key=lambda row: (row["impact_score"], row["name"]), reverse=True)


def service_impact(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        status = str(item.get("Status") or item.get("status") or "")
        start_type = str(item.get("StartType") or item.get("start_type") or "")
        if status.lower() != "running":
            continue
        score = 1
        reasons = ["currently running"]
        if start_type.lower() in {"automatic", "auto"}:
            score += 1
            reasons.append("automatic start")
        rows.append(
            {
                "name": item.get("Name") or item.get("name") or "unknown",
                "display_name": item.get("DisplayName") or item.get("display_name"),
                "status": status,
                "start_type": start_type,
                "impact_score": score,
                "reasons": reasons,
                "approval_required": True,
                "safe_action": "inspect dependency and vendor before changing service startup",
            }
        )
    return sorted(rows, key=lambda row: (row["impact_score"], row["name"]), reverse=True)


def collect_process_samples(sample_seconds: float = 1.0, limit: int = 80) -> list[dict[str, Any]]:
    sample_seconds = max(0.1, min(float(sample_seconds), 10.0))
    limit = _bounded_int(limit, 1, 300)
    procs = list(psutil.process_iter(["pid", "name", "username", "memory_info", "num_threads", "cmdline"]))
    for proc in procs:
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(sample_seconds)
    rows = []
    for proc in procs:
        try:
            info = proc.info
            mem = info.get("memory_info")
            rows.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name"),
                    "username": info.get("username"),
                    "rss_mb": _mb(mem.rss if mem else 0),
                    "cpu_percent": round(proc.cpu_percent(None), 1),
                    "threads": info.get("num_threads") or 0,
                    "cmdline": " ".join(info.get("cmdline") or [])[:500],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(rows, key=lambda row: (row["cpu_percent"], row["rss_mb"]), reverse=True)[:limit]


def collect_counters(sample_seconds: float = 1.0) -> dict[str, Any]:
    sample_seconds = max(0.1, min(float(sample_seconds), 10.0))
    disk_before = psutil.disk_io_counters()
    net_before = psutil.net_io_counters()
    cpu = psutil.cpu_percent(interval=sample_seconds, percpu=True)
    disk_after = psutil.disk_io_counters()
    net_after = psutil.net_io_counters()
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    return {
        "sample_seconds": sample_seconds,
        "cpu": {
            "overall_percent": round(sum(cpu) / len(cpu), 1) if cpu else 0.0,
            "per_cpu_percent": cpu,
            "logical_count": psutil.cpu_count(logical=True),
            "physical_count": psutil.cpu_count(logical=False),
        },
        "memory": {
            "total_gb": _gb(vm.total),
            "available_gb": _gb(vm.available),
            "used_gb": _gb(vm.used),
            "percent": vm.percent,
        },
        "swap": {"total_gb": _gb(sm.total), "used_gb": _gb(sm.used), "percent": sm.percent},
        "disk_io_per_sec": _counter_delta(disk_before, disk_after, sample_seconds),
        "network_io_per_sec": _counter_delta(net_before, net_after, sample_seconds),
    }


def _counter_delta(before: Any, after: Any, seconds: float) -> dict[str, float]:
    if not before or not after:
        return {}
    rows = {}
    for field in getattr(after, "_fields", []):
        previous = getattr(before, field, 0) or 0
        current = getattr(after, field, 0) or 0
        rows[field] = round((current - previous) / seconds, 1)
    return rows


def tcp_latency(host: str, port: int, timeout: float = 2.0, count: int = 3) -> dict[str, Any]:
    count = _bounded_int(count, 1, 10)
    timeout = max(0.2, min(float(timeout), 10.0))
    attempts = []
    for _ in range(count):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                attempts.append({"ok": True, "duration_ms": round((time.perf_counter() - started) * 1000, 1), "error": None})
        except OSError as exc:
            attempts.append({"ok": False, "duration_ms": round((time.perf_counter() - started) * 1000, 1), "error": str(exc)})
    ok_count = sum(1 for attempt in attempts if attempt["ok"])
    return {
        "host": host,
        "port": int(port),
        "count": count,
        "ok_count": ok_count,
        "success_percent": _percent(ok_count, count),
        "attempts": attempts,
        "summary": summarize_durations([attempt for attempt in attempts if attempt["ok"]]),
    }


def homelab_latency(targets: list[dict[str, Any]] | None = None, count: int = 3, timeout: float = 2.0) -> dict[str, Any]:
    rows = []
    for target in targets or DEFAULT_LATENCY_TARGETS:
        row = tcp_latency(str(target["host"]), int(target.get("port", 80)), timeout=timeout, count=count)
        row["name"] = target.get("name") or f"{target['host']}:{target.get('port', 80)}"
        rows.append(row)
    return {"targets": rows}


def safe_recommendations(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    counters = snapshot.get("counters") or {}
    memory = counters.get("memory") or {}
    cpu = counters.get("cpu") or {}
    if float(memory.get("percent") or 0) >= 85:
        recommendations.append(
            {
                "area": "memory",
                "severity": "medium",
                "finding": "Memory pressure is high during this sample.",
                "safe_next_step": "Review top process groups and close or reconfigure user apps only after approval.",
                "approval_required": True,
            }
        )
    if float(cpu.get("overall_percent") or 0) >= 80:
        recommendations.append(
            {
                "area": "cpu",
                "severity": "medium",
                "finding": "CPU utilization is high during this sample.",
                "safe_next_step": "Take another sample, then inspect the highest CPU process groups before any action.",
                "approval_required": True,
            }
        )
    for target in (snapshot.get("latency") or {}).get("targets", []):
        if float(target.get("success_percent") or 0) < 100:
            recommendations.append(
                {
                    "area": "network",
                    "severity": "low",
                    "finding": f"{target.get('name')} had failed TCP latency attempts.",
                    "safe_next_step": "Compare with router/server health before changing routes, DNS, or firewall settings.",
                    "approval_required": True,
                }
            )
    for group in snapshot.get("process_groups") or []:
        if int(group.get("impact_score") or 0) >= 4:
            recommendations.append(
                {
                    "area": "process",
                    "severity": "low",
                    "finding": f"{group.get('name')} is a notable sampled resource consumer.",
                    "safe_next_step": group.get("safe_action"),
                    "approval_required": True,
                }
            )
    return recommendations


def performance_snapshot(
    sample_seconds: float = 1.0,
    process_limit: int = 80,
    latency_count: int = 2,
    latency_timeout: float = 2.0,
) -> dict[str, Any]:
    counters = collect_counters(sample_seconds)
    processes = collect_process_samples(sample_seconds=sample_seconds, limit=process_limit)
    total_memory_mb = float((counters.get("memory") or {}).get("total_gb") or 0) * 1024
    snapshot = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "read_only": True,
        "counters": counters,
        "processes": processes,
        "process_groups": process_group_impact(processes, total_memory_mb, limit=25),
        "latency": homelab_latency(count=latency_count, timeout=latency_timeout),
    }
    snapshot["recommendations"] = safe_recommendations(snapshot)
    return snapshot
