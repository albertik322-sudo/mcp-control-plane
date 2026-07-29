from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit


DOC_PATHS = {
    "INFRASTRUCTURE.md": "/opt/homelab-observability/docs/INFRASTRUCTURE.md",
    "RUNBOOK.md": "/opt/homelab-observability/docs/RUNBOOK.md",
}

KNOWN_OPTIONAL_CONTAINERS = {
    "ollama",
    "nc_app_context_chat_backend",
    "nc_app_text2speech_kokoro",
    "nc_app_text2image_stablediffusion2",
    "nc_app_stt_whisper2",
}

SYSTEMD_UNIT_POLICIES: dict[str, dict[str, Any]] = {
    "dcvserver.service": {
        "expected_live": False,
        "lifecycle": "retired",
        "reason": "intentionally retained but disabled",
    },
}

_SYSTEMD_UNIT_RE = re.compile(r"\b([a-zA-Z0-9_.@-]+\.(?:service|timer))\b")
_NON_LIVE_LIFECYCLE_RE = re.compile(r"\b(?:decommissioned|disabled|masked|removed|retired)\b", re.I)
_EXTERNAL_SYSTEMD_RE = re.compile(
    r"\b(?:external|remote)\s+(?:vpn\s+)?(?:hosts?|nodes?|servers?|vps|components?|files?|units?|services?|timers?)\b"
    r"|\bexternal\s+vpn\b"
    r"|\bvps(?:-only|\s+(?:components|hosts?|nodes?|servers?|checks?))\b"
    r"|\bon\s+(?:a|all|every)\s+vps\b"
    r"|\binstalled\s+on\s+all\s+remote\b"
    r"|\bchanges\s+applied\s+on\s+(?:wg|xray|vms|frankfurt|latvia|ru|cascade)\b",
    re.I,
)
_HOME_SYSTEMD_RE = re.compile(r"\bhome(?:-server|\s+(?:components|server|checks?))\b", re.I)
_DOMAIN_SUFFIXES = ("example.com", "lan")


@dataclass(frozen=True)
class Finding:
    id: str
    area: str
    severity: str
    kind: str
    expected: str
    actual: str
    suggestion: str

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


def _set(values: list[str] | set[str]) -> set[str]:
    return {str(value).strip().strip("`") for value in values if str(value).strip()}


def _regex_set(pattern: str, text: str, flags: int = 0) -> set[str]:
    out: set[str] = set()
    for match in re.findall(pattern, text, flags=flags):
        if isinstance(match, tuple):
            out.update(part.strip() for part in match if part and part.strip())
        elif match and str(match).strip():
            out.add(str(match).strip())
    return out


def _section(markdown: str, heading_pattern: str) -> str:
    match = re.search(heading_pattern, markdown, flags=re.I)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"\n#{1,3}\s+", markdown[start:])
    end = start + next_heading.start() if next_heading else len(markdown)
    return markdown[start:end]


def _systemd_scope(context: str) -> str | None:
    context = context.replace("`", "")
    if _EXTERNAL_SYSTEMD_RE.search(context):
        return "external"
    if _HOME_SYSTEMD_RE.search(context):
        return "home"
    return None


def _extract_systemd_inventory(text: str) -> tuple[set[str], set[str], dict[str, dict[str, Any]]]:
    units: set[str] = set()
    expected_home_units: set[str] = set()
    metadata: dict[str, dict[str, Any]] = {}
    heading = ""
    block_scope = "home"

    for line in text.splitlines():
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading_match:
            heading = heading_match.group(1)
            block_scope = _systemd_scope(heading) or "home"
            continue

        unit_names = set(_SYSTEMD_UNIT_RE.findall(line))
        if not unit_names:
            block_scope = _systemd_scope(f"{heading} {line}") or block_scope
            continue

        scope = _systemd_scope(f"{heading} {line}") or block_scope
        lifecycle = "non_live" if _NON_LIVE_LIFECYCLE_RE.search(f"{heading} {line}") else "expected"
        for unit in unit_names:
            units.add(unit)
            item = metadata.setdefault(unit, {"scopes": set(), "lifecycles": set()})
            item["scopes"].add(scope)
            item["lifecycles"].add(lifecycle)
            if scope == "home" and lifecycle == "expected":
                expected_home_units.add(unit)

    for unit, policy in SYSTEMD_UNIT_POLICIES.items():
        if unit not in units:
            continue
        item = metadata.setdefault(unit, {"scopes": set(), "lifecycles": set()})
        item["policy"] = dict(policy)
        if not policy.get("expected_live", True):
            expected_home_units.discard(unit)

    for unit, item in metadata.items():
        if "external" in item["scopes"]:
            expected_home_units.discard(unit)

    normalized_metadata: dict[str, dict[str, Any]] = {}
    for unit, item in metadata.items():
        normalized = {
            "scopes": sorted(item["scopes"]),
            "lifecycles": sorted(item["lifecycles"]),
        }
        if "policy" in item:
            normalized["policy"] = item["policy"]
        normalized_metadata[unit] = normalized
    return units, expected_home_units, normalized_metadata


def _is_inventory_domain(hostname: str | None) -> bool:
    if not hostname:
        return False
    hostname = hostname.rstrip(".").lower()
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in _DOMAIN_SUFFIXES)


def _domains_from_endpoint(value: Any) -> set[str]:
    pending = [str(value).strip()]
    seen: set[str] = set()
    domains: set[str] = set()

    while pending:
        candidate = pending.pop()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)

        parsed = urlsplit(candidate)
        if parsed.scheme.lower() in {"http", "https"}:
            if _is_inventory_domain(parsed.hostname):
                domains.add(str(parsed.hostname).rstrip(".").lower())
            pending.extend(query_value for _, query_value in parse_qsl(parsed.query, keep_blank_values=False))
            continue

        decoded = unquote(candidate)
        if decoded != candidate and decoded.lower().startswith(("http://", "https://")):
            pending.append(decoded)
            continue

        if not any(char in candidate for char in "/?#%"):
            bare = urlsplit(f"//{candidate}")
            if _is_inventory_domain(bare.hostname):
                domains.add(str(bare.hostname).rstrip(".").lower())

    return domains


def load_docs() -> dict[str, str]:
    import server

    docs: dict[str, str] = {}
    for name, path in DOC_PATHS.items():
        result = server.read_file(path, max_bytes=700_000)
        docs[name] = str(result.get("content") or result.get("stdout") or result)
    return docs


def extract_doc_inventory(docs: dict[str, str]) -> dict[str, Any]:
    text = "\n".join(docs.values())
    urls = _set(re.findall(r"https?://[^\s)`|>]+", text))
    domains = _set(re.findall(r"\b(?:[a-z0-9-]+\.)+(?:example\.com|example\.lan|lab\.lan|lan)\b", text, flags=re.I))
    paths = _set(re.findall(r"`(/(?:opt|etc|var|usr|mnt|backup|root|home)[^` \n|]*)`", text))
    compose_paths = {path for path in paths if path.endswith(("docker-compose.yml", "compose.yml", "compose.yaml"))}
    systemd_units, home_systemd_units, systemd_unit_metadata = _extract_systemd_inventory(text)
    container_sections = "\n".join(
        [
            _section(text, r"\n#{2,3}\s+Running Containers\b"),
            _section(text, r"\n#{2,3}\s+Docker On Home Server\b"),
        ]
    )
    containers = set()
    for line in container_sections.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if not cells or cells[0].lower() in {"container", "project", "---"}:
            continue
        value = cells[0]
        if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", value) and "." not in value:
            containers.add(value)
    ports = _set(re.findall(r"\b(?:127\.0\.0\.1|0\.0\.0\.0|192\.168\.88\.238)?:?([1-9][0-9]{2,4})\b", text))
    return {
        "urls": sorted(urls),
        "domains": sorted(domains),
        "paths": sorted(paths),
        "compose_paths": sorted(compose_paths),
        "systemd_units": sorted(systemd_units),
        "home_systemd_units": sorted(home_systemd_units),
        "systemd_unit_metadata": systemd_unit_metadata,
        "containers": sorted(containers),
        "ports": sorted(ports),
        "doc_files": sorted(docs),
    }


def collect_live_inventory() -> dict[str, Any]:
    import docker_mcp
    import homelab_mcp
    import server

    live: dict[str, Any] = {}
    live["docker_ps"] = docker_mcp.docker_ps()
    live["compose_projects"] = docker_mcp.docker_compose_projects()
    live["systemd"] = server._exec("systemctl list-units --type=service,timer --all --no-pager --plain", timeout=30, sudo=True)
    live["apache"] = server._exec("apache2ctl -S 2>&1; ls -1 /etc/apache2/sites-enabled 2>/dev/null", timeout=30, sudo=True)
    live["ports"] = server.ports_list()
    live["prometheus_targets"] = homelab_mcp.prometheus_targets()
    live["uptime_kuma"] = homelab_mcp.uptime_kuma_snapshot()
    return normalize_live_inventory(live)


def normalize_live_inventory(raw: dict[str, Any]) -> dict[str, Any]:
    docker_text = json.dumps(raw.get("docker_ps", {}), ensure_ascii=False, default=str)
    compose_text = json.dumps(raw.get("compose_projects", {}), ensure_ascii=False, default=str)
    systemd_text = str(raw.get("systemd", {}).get("stdout", ""))
    apache_text = str(raw.get("apache", {}).get("stdout", ""))
    ports_text = json.dumps(raw.get("ports", {}), ensure_ascii=False, default=str)

    targets = raw.get("prometheus_targets", {}).get("data", {}).get("activeTargets", [])
    kuma_monitors = raw.get("uptime_kuma", {}).get("monitors", [])
    urls = set()
    domains = set()
    for target in targets:
        scrape_url = target.get("scrapeUrl") or target.get("labels", {}).get("__address__")
        if scrape_url:
            urls.add(str(scrape_url))
    for mon in kuma_monitors or []:
        url = mon.get("url") or mon.get("hostname")
        if url:
            urls.add(str(url))
    for url in urls:
        domains.update(_domains_from_endpoint(url))

    return {
        "containers": sorted(_regex_set(r'"Names"\s*:\s*"([^"]+)"|"name"\s*:\s*"([^"]+)"|\b([a-zA-Z0-9][a-zA-Z0-9_.-]{2,})\s+(?:Up|Exited|Created)', docker_text)),
        "compose_paths": sorted(_set(re.findall(r"/opt/[^\s\"']+(?:docker-compose|compose)\.ya?ml", compose_text))),
        "systemd_units": sorted(_set(re.findall(r"\b([a-zA-Z0-9_.@-]+\.(?:service|timer))\b", systemd_text))),
        "apache_vhosts": sorted(_regex_set(r"namevhost\s+([^\s]+)|alias\s+([a-zA-Z0-9.-]+)", apache_text)),
        "ports": sorted(_set(re.findall(r":([1-9][0-9]{2,4})\b", ports_text))),
        "monitor_urls": sorted(urls),
        "monitor_domains": sorted(domains),
    }


def compare_inventory(doc: dict[str, Any], live: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[Finding] = []

    doc_containers = set(doc["containers"])
    live_containers = set(live["containers"])
    for name in sorted(doc_containers - live_containers):
        if name in KNOWN_OPTIONAL_CONTAINERS:
            severity = "info"
            kind = "known_exception"
        else:
            severity = "warning"
            kind = "missing_live"
        findings.append(Finding(f"docker.container.doc_only:{name}", "docker", severity, kind, name, "not found in docker ps snapshot", "Verify container name or update docs/policy."))
    for name in sorted(live_containers - doc_containers):
        if name in KNOWN_OPTIONAL_CONTAINERS:
            continue
        findings.append(Finding(f"docker.container.live_only:{name}", "docker", "info", "missing_doc", "documented container", name, "Document this container or add it to policy."))

    doc_units = set(doc.get("home_systemd_units", doc["systemd_units"]))
    live_units = set(live["systemd_units"])
    for unit in sorted(doc_units - live_units):
        findings.append(Finding(f"systemd.unit.doc_only:{unit}", "systemd", "warning", "missing_live", unit, "not found in systemctl list-units", "Check unit state or update docs."))

    doc_domains = {d.lower() for d in doc["domains"]}
    monitor_domains = {d.lower() for d in live["monitor_domains"]}
    for domain in sorted(doc_domains - monitor_domains):
        if domain.endswith(".lan") or domain.endswith("example.com"):
            findings.append(Finding(f"monitor.domain.gap:{domain}", "observability", "info", "monitor_gap", domain, "not seen in Prometheus/Kuma monitor URLs", "Add monitoring if this endpoint should be watched."))
    for domain in sorted(monitor_domains - doc_domains):
        findings.append(Finding(f"monitor.domain.live_only:{domain}", "documentation", "info", "missing_doc", "documented endpoint", domain, "Add the monitored endpoint to INFRASTRUCTURE/RUNBOOK or policy."))

    doc_compose = set(doc["compose_paths"])
    live_compose = set(live["compose_paths"])
    for path in sorted(doc_compose - live_compose):
        findings.append(Finding(f"compose.path.doc_only:{path}", "docker", "info", "missing_live", path, "not found in compose project scan", "Confirm whether compose path is inactive or scanner missed it."))

    return [finding.as_dict() for finding in findings]


def snapshot() -> dict[str, Any]:
    docs = load_docs()
    doc_inventory = extract_doc_inventory(docs)
    live_inventory = collect_live_inventory()
    return {"docs": doc_inventory, "live": live_inventory}


def check() -> dict[str, Any]:
    snap = snapshot()
    findings = compare_inventory(snap["docs"], snap["live"])
    severities: dict[str, int] = {}
    for finding in findings:
        severities[finding["severity"]] = severities.get(finding["severity"], 0) + 1
    return {"status": "ok", "finding_count": len(findings), "severities": severities, "findings": findings, "snapshot": snap}


def render_markdown_report(result: dict[str, Any] | None = None) -> str:
    result = result or check()
    lines = [
        "# Homelab Doc Drift Report",
        "",
        f"Findings: {result.get('finding_count', 0)}",
        f"Severities: {json.dumps(result.get('severities', {}), ensure_ascii=False, sort_keys=True)}",
        "",
    ]
    for item in result.get("findings", [])[:80]:
        lines.append(f"- **{item['severity']}** `{item['id']}`: expected `{item['expected']}`, actual `{item['actual']}`. {item['suggestion']}")
    return "\n".join(lines)
