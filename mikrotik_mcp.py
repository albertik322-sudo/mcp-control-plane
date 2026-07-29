from __future__ import annotations

import os
import shlex
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any

import paramiko
from dotenv import load_dotenv
from mcp_runtime import compact_command_result, configure_stdio, create_mcp_server


configure_stdio()
load_dotenv(encoding="utf-8-sig")
mcp = create_mcp_server("mikrotik-routeros")


@dataclass(frozen=True)
class MikroTikConfig:
    host: str
    port: int
    user: str
    password: str
    use_tls: bool
    timeout: float


def _config(api_port: int | None = None, use_tls: bool | None = None) -> MikroTikConfig:
    host = os.getenv("MIKROTIK_HOST", "192.168.1.1").strip()
    user = os.getenv("MIKROTIK_USER", "admin").strip()
    password = os.getenv("MIKROTIK_PASSWORD", "")
    tls = (os.getenv("MIKROTIK_USE_TLS", "0").strip().lower() in {"1", "true", "yes"})
    if use_tls is not None:
        tls = use_tls
    port = api_port or int(os.getenv("MIKROTIK_PORT", "8729" if tls else "8728"))
    return MikroTikConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        use_tls=tls,
        timeout=float(os.getenv("MIKROTIK_TIMEOUT", "10")),
    )


def _write_len(sock: socket.socket, length: int) -> None:
    if length < 0x80:
        data = bytes([length])
    elif length < 0x4000:
        data = bytes([(length >> 8) | 0x80, length & 0xFF])
    elif length < 0x200000:
        data = bytes([(length >> 16) | 0xC0, (length >> 8) & 0xFF, length & 0xFF])
    elif length < 0x10000000:
        data = bytes([(length >> 24) | 0xE0, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    else:
        data = bytes([0xF0, (length >> 24) & 0xFF, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    sock.sendall(data)


def _read_len(sock: socket.socket) -> int:
    first = sock.recv(1)
    if not first:
        raise ConnectionError("RouterOS API closed the connection")
    c = first[0]
    if (c & 0x80) == 0:
        return c
    if (c & 0xC0) == 0x80:
        return ((c & ~0xC0) << 8) + sock.recv(1)[0]
    if (c & 0xE0) == 0xC0:
        b = sock.recv(2)
        return ((c & ~0xE0) << 16) + (b[0] << 8) + b[1]
    if (c & 0xF0) == 0xE0:
        b = sock.recv(3)
        return ((c & ~0xF0) << 24) + (b[0] << 16) + (b[1] << 8) + b[2]
    b = sock.recv(4)
    return (b[0] << 24) + (b[1] << 16) + (b[2] << 8) + b[3]


def _write_word(sock: socket.socket, word: str) -> None:
    raw = word.encode("utf-8")
    _write_len(sock, len(raw))
    sock.sendall(raw)


def _read_word(sock: socket.socket) -> str:
    length = _read_len(sock)
    if length == 0:
        return ""
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("RouterOS API closed the connection while reading a word")
        data += chunk
    return data.decode("utf-8", errors="replace")


def _send_sentence(sock: socket.socket, words: list[str]) -> None:
    for word in words:
        _write_word(sock, word)
    _write_word(sock, "")


def _read_sentence(sock: socket.socket) -> list[str]:
    words: list[str] = []
    while True:
        word = _read_word(sock)
        if word == "":
            return words
        words.append(word)


def _parse_reply(sentences: list[list[str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sentence in sentences:
        if not sentence:
            continue
        row: dict[str, Any] = {"reply": sentence[0]}
        for word in sentence[1:]:
            if word.startswith("=ret="):
                row["ret"] = word[5:]
            elif word.startswith("=message="):
                row["message"] = word[9:]
            elif word.startswith("="):
                key, _, value = word[1:].partition("=")
                row[key] = value
            else:
                row.setdefault("words", []).append(word)
        rows.append(row)
    return rows


class RouterOSApi:
    def __init__(self, cfg: MikroTikConfig):
        raw = socket.create_connection((cfg.host, cfg.port), timeout=cfg.timeout)
        raw.settimeout(cfg.timeout)
        if cfg.use_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.sock = context.wrap_socket(raw, server_hostname=cfg.host)
        else:
            self.sock = raw

    def close(self) -> None:
        self.sock.close()

    def talk(self, words: list[str]) -> list[dict[str, Any]]:
        _send_sentence(self.sock, words)
        sentences: list[list[str]] = []
        while True:
            sentence = _read_sentence(self.sock)
            sentences.append(sentence)
            if sentence and sentence[0] in {"!done", "!fatal"}:
                break
        return _parse_reply(sentences)

    def login(self, user: str, password: str) -> list[dict[str, Any]]:
        return self.talk(["/login", f"=name={user}", f"=password={password}"])


def _api(words: list[str], api_port: int | None = None, use_tls: bool | None = None) -> list[dict[str, Any]]:
    cfg = _config(api_port, use_tls)
    client = RouterOSApi(cfg)
    try:
        login = client.login(cfg.user, cfg.password)
        if any(row.get("reply") in {"!trap", "!fatal"} for row in login):
            return [{"reply": "!fatal", "message": f"login failed: {login}"}]
        return client.talk(words)
    finally:
        client.close()


def _ok(rows: list[dict[str, Any]]) -> bool:
    return not any(row.get("reply") in {"!trap", "!fatal"} for row in rows)


def _first_data(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        if row.get("reply") == "!re":
            return row
    return {}


def _data_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("reply") == "!re"]


def _api_first_available(commands: list[list[str]]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for words in commands:
        rows = _api(words)
        attempts.append({"words": words, "ok": _ok(rows), "rows": rows})
        if _ok(rows):
            return {"words": words, "rows": rows, "attempts": attempts}
    return {"rows": attempts[-1]["rows"] if attempts else [], "attempts": attempts}


def _terminal_to_api(command: str) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError("command must not be empty")
    if parts[0].startswith("/"):
        path_parts = [parts[0].strip("/")]
        idx = 1
    else:
        path_parts = []
        idx = 0
    while idx < len(parts) and "=" not in parts[idx] and parts[idx] not in {"print", "add", "set", "remove", "enable", "disable", "find", "export"}:
        path_parts.append(parts[idx].strip("/"))
        idx += 1
    action = parts[idx] if idx < len(parts) and "=" not in parts[idx] else "print"
    idx += 1 if idx < len(parts) and parts[idx] == action else 0
    words = ["/" + "/".join(path_parts + [action])]
    for item in parts[idx:]:
        if item.startswith("?") or item.startswith("."):
            words.append(item)
        elif "=" in item:
            key, value = item.split("=", 1)
            words.append(f"={key.lstrip('=')}={value}")
        else:
            words.append(item)
    return words


@mcp.tool()
def probe() -> dict[str, Any]:
    """Check common RouterOS management ports without logging in."""
    cfg = _config()
    ports = [22, 80, 443, 8291, 8728, 8729]
    results: dict[str, bool] = {}
    for port in ports:
        try:
            with socket.create_connection((cfg.host, port), timeout=2):
                results[str(port)] = True
        except OSError:
            results[str(port)] = False
    return {"host": cfg.host, "ports": results}


@mcp.tool()
def api_sentence(words: list[str], api_port: int | None = None, use_tls: bool | None = None) -> dict[str, Any]:
    """Run a raw RouterOS API sentence, for example ['/system/resource/print']."""
    if not words or not words[0].startswith("/"):
        raise ValueError("words must start with a RouterOS API command path")
    started = time.time()
    return {"rows": _api(words, api_port, use_tls), "duration_seconds": round(time.time() - started, 3)}


@mcp.tool()
def run(command: str) -> dict[str, Any]:
    """Run a terminal-like RouterOS command through the API, for example '/system resource print'."""
    words = _terminal_to_api(command)
    started = time.time()
    return {"api_words": words, "rows": _api(words), "duration_seconds": round(time.time() - started, 3)}


@mcp.tool()
def resource() -> dict[str, Any]:
    """Return /system/resource/print."""
    return {"rows": _api(["/system/resource/print"])}


@mcp.tool()
def interfaces() -> dict[str, Any]:
    """Return /interface/print."""
    return {"rows": _api(["/interface/print"])}


@mcp.tool()
def ip_addresses() -> dict[str, Any]:
    """Return /ip/address/print."""
    return {"rows": _api(["/ip/address/print"])}


@mcp.tool()
def routes() -> dict[str, Any]:
    """Return /ip/route/print."""
    return {"rows": _api(["/ip/route/print"])}


@mcp.tool()
def firewall_filters() -> dict[str, Any]:
    """Return /ip/firewall/filter/print."""
    return {"rows": _api(["/ip/firewall/filter/print"])}


@mcp.tool()
def nat_rules() -> dict[str, Any]:
    """Return /ip/firewall/nat/print."""
    return {"rows": _api(["/ip/firewall/nat/print"])}


@mcp.tool()
def dhcp_leases() -> dict[str, Any]:
    """Return /ip/dhcp-server/lease/print."""
    return {"rows": _api(["/ip/dhcp-server/lease/print"])}


@mcp.tool()
def safe_snapshot() -> dict[str, Any]:
    """Collect a read-only operational snapshot from common RouterOS menus."""
    commands = {
        "resource": ["/system/resource/print"],
        "identity": ["/system/identity/print"],
        "interfaces": ["/interface/print"],
        "addresses": ["/ip/address/print"],
        "routes": ["/ip/route/print"],
        "dns": ["/ip/dns/print"],
        "services": ["/ip/service/print"],
        "packages": ["/system/package/print"],
        "clock": ["/system/clock/print"],
        "neighbors": ["/ip/neighbor/print"],
        "arp": ["/ip/arp/print"],
        "dhcp_leases": ["/ip/dhcp-server/lease/print"],
        "firewall_filter": ["/ip/firewall/filter/print"],
        "firewall_nat": ["/ip/firewall/nat/print"],
        "address_lists": ["/ip/firewall/address-list/print"],
        "wireguard_peers": ["/interface/wireguard/peers/print"],
    }
    return {name: _api(words) for name, words in commands.items()}


@mcp.tool()
def router_logs(topics: str | None = None) -> dict[str, Any]:
    """Return RouterOS logs, optionally filtered by topics."""
    words = ["/log/print"]
    if topics:
        words.append(f"?topics={topics}")
    return {"rows": _api(words)}


@mcp.tool()
def ip_services() -> dict[str, Any]:
    """Return /ip/service/print."""
    return {"rows": _api(["/ip/service/print"])}


@mcp.tool()
def users() -> dict[str, Any]:
    """Return /user/print."""
    return {"rows": _api(["/user/print"])}


@mcp.tool()
def packages() -> dict[str, Any]:
    """Return /system/package/print."""
    return {"rows": _api(["/system/package/print"])}


@mcp.tool()
def wifi_registration() -> dict[str, Any]:
    """Return registered Wi-Fi clients, trying RouterOS 7 WiFi then legacy wireless."""
    return _api_first_available(
        [
            ["/interface/wifi/registration-table/print"],
            ["/interface/wireless/registration-table/print"],
        ]
    )


@mcp.tool()
def wireguard_peers() -> dict[str, Any]:
    """Return WireGuard peers."""
    return {"rows": _api(["/interface/wireguard/peers/print"])}


@mcp.tool()
def bridge_ports() -> dict[str, Any]:
    """Return bridge ports."""
    return {"rows": _api(["/interface/bridge/port/print"])}


@mcp.tool()
def firewall_address_lists() -> dict[str, Any]:
    """Return firewall address lists."""
    return {"rows": _api(["/ip/firewall/address-list/print"])}


@mcp.tool()
def dns_settings() -> dict[str, Any]:
    """Return DNS settings."""
    return {"rows": _api(["/ip/dns/print"])}


@mcp.tool()
def neighbors() -> dict[str, Any]:
    """Return MikroTik neighbor discovery table."""
    return {"rows": _api(["/ip/neighbor/print"])}


@mcp.tool()
def arp_table() -> dict[str, Any]:
    """Return ARP table."""
    return {"rows": _api(["/ip/arp/print"])}


@mcp.tool()
def dhcp_servers() -> dict[str, Any]:
    """Return DHCP server definitions and networks."""
    return {
        "servers": _api(["/ip/dhcp-server/print"]),
        "networks": _api(["/ip/dhcp-server/network/print"]),
    }


@mcp.tool()
def dns_cache(limit: int = 100) -> dict[str, Any]:
    """Return RouterOS DNS cache entries."""
    rows = _api(["/ip/dns/cache/print"])
    return {"rows": _data_rows(rows)[: max(1, min(int(limit), 1000))], "raw": rows}


@mcp.tool()
def connections(limit: int = 100, dst_address: str | None = None, src_address: str | None = None) -> dict[str, Any]:
    """Return connection tracking rows, optionally filtered by source or destination address substring."""
    rows = _data_rows(_api(["/ip/firewall/connection/print"]))
    if dst_address:
        rows = [row for row in rows if dst_address in str(row.get("dst-address", ""))]
    if src_address:
        rows = [row for row in rows if src_address in str(row.get("src-address", ""))]
    return {"connections": rows[: max(1, min(int(limit), 1000))], "total_matching": len(rows)}


@mcp.tool()
def interface_traffic(interface: str, once: bool = True) -> dict[str, Any]:
    """Monitor interface traffic counters."""
    if not interface or any(ch in interface for ch in "\r\n"):
        raise ValueError("invalid interface")
    words = ["/interface/monitor-traffic", f"=interface={interface}"]
    if once:
        words.append("=once=")
    return {"rows": _api(words)}


@mcp.tool()
def router_health() -> dict[str, Any]:
    """Return a compact RouterOS health summary with computed warnings."""
    resource_row = _first_data(_api(["/system/resource/print"]))
    services = _data_rows(_api(["/ip/service/print"]))
    interfaces_rows = _data_rows(_api(["/interface/print"]))
    routes_rows = _data_rows(_api(["/ip/route/print"]))
    dns_row = _first_data(_api(["/ip/dns/print"]))
    warnings: list[str] = []

    for key, label in (("free-hdd-space", "free disk"), ("free-memory", "free memory")):
        value = str(resource_row.get(key, ""))
        if value.endswith("KiB"):
            try:
                if float(value[:-3]) < 65536:
                    warnings.append(f"Low {label}: {value}")
            except ValueError:
                pass

    insecure_enabled = [
        row.get("name")
        for row in services
        if row.get("disabled") != "true" and row.get("name") in {"telnet", "ftp", "www", "api"}
    ]
    if insecure_enabled:
        warnings.append("Insecure management services enabled: " + ", ".join(str(x) for x in insecure_enabled))

    down_interfaces = [
        row.get("name")
        for row in interfaces_rows
        if row.get("disabled") != "true" and row.get("running") == "false"
    ]
    default_routes = [row for row in routes_rows if row.get("dst-address") == "0.0.0.0/0" or row.get("dst-address") == "::/0"]
    return {
        "resource": resource_row,
        "identity": _first_data(_api(["/system/identity/print"])),
        "clock": _first_data(_api(["/system/clock/print"])),
        "dns": dns_row,
        "interfaces": {
            "total": len(interfaces_rows),
            "running": sum(1 for row in interfaces_rows if row.get("running") == "true"),
            "down_enabled": down_interfaces,
        },
        "routes": {"default_routes": default_routes, "total": len(routes_rows)},
        "management_services": services,
        "warnings": warnings,
    }


@mcp.tool()
def security_audit() -> dict[str, Any]:
    """Audit common RouterOS hardening items without making changes."""
    services = _data_rows(_api(["/ip/service/print"]))
    users_rows = _data_rows(_api(["/user/print"]))
    dns = _first_data(_api(["/ip/dns/print"]))
    firewall = _data_rows(_api(["/ip/firewall/filter/print"]))
    recommendations: list[str] = []

    risky_services = []
    for row in services:
        if row.get("disabled") == "true":
            continue
        name = row.get("name", "")
        address = row.get("address", "")
        if name in {"telnet", "ftp", "www", "api"}:
            risky_services.append(row)
            recommendations.append(f"Disable or restrict {name}; prefer SSH/Winbox/API-SSL with address allowlists.")
        elif not address:
            recommendations.append(f"Restrict management service {name} to trusted LAN/admin subnets.")

    if str(dns.get("allow-remote-requests", "")).lower() == "true":
        recommendations.append("DNS recursive service is enabled; ensure firewall blocks WAN queries.")

    if any(row.get("name") == "admin" for row in users_rows):
        recommendations.append("Rename or disable the default 'admin' account after creating a named admin user.")

    has_drop_invalid = any(
        row.get("chain") == "input" and row.get("connection-state") == "invalid" and row.get("action") == "drop"
        for row in firewall
    )
    if not has_drop_invalid:
        recommendations.append("Add an input-chain rule to drop invalid connections before accepts.")

    return {
        "services": services,
        "users": users_rows,
        "dns": dns,
        "firewall_filter_rule_count": len(firewall),
        "risky_services": risky_services,
        "recommendations": recommendations,
    }


@mcp.tool()
def disable_ip_service(name: str) -> dict[str, Any]:
    """Disable a RouterOS /ip/service entry by name."""
    allowed = {"telnet", "ftp", "www", "www-ssl", "ssh", "api", "api-ssl", "winbox"}
    if name not in allowed:
        raise ValueError(f"name must be one of {sorted(allowed)}")
    return {"rows": _api(["/ip/service/disable", f"=.id={name}"])}


@mcp.tool()
def set_ip_service_addresses(name: str, address: str) -> dict[str, Any]:
    """Restrict a RouterOS /ip/service entry to an address allowlist."""
    allowed = {"telnet", "ftp", "www", "www-ssl", "ssh", "api", "api-ssl", "winbox"}
    if name not in allowed:
        raise ValueError(f"name must be one of {sorted(allowed)}")
    if any(ch in address for ch in "\r\n;"):
        raise ValueError("invalid address")
    return {"rows": _api(["/ip/service/set", f"=.id={name}", f"=address={address}"])}


@mcp.tool()
def containers() -> dict[str, Any]:
    """Return RouterOS containers."""
    return {"rows": _api(["/container/print"])}


@mcp.tool()
def ping(address: str, count: int = 4, timeout: int = 20) -> dict[str, Any]:
    """Run RouterOS ping over SSH."""
    return ssh(f"/ping {address} count={int(count)}", timeout=timeout)


@mcp.tool()
def traceroute(address: str, timeout: int = 30) -> dict[str, Any]:
    """Run RouterOS traceroute over SSH."""
    return ssh(f"/tool traceroute {address}", timeout=timeout)


@mcp.tool()
def backup_config(name: str = "codex-backup") -> dict[str, Any]:
    """Create a RouterOS backup file on the router."""
    safe = "".join(c for c in name if c.isalnum() or c in {"-", "_"})
    if not safe:
        safe = "codex-backup"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return ssh(f"/system backup save name={safe}-{stamp}", timeout=30)


@mcp.tool()
def export_config(
    hide_sensitive: bool = True,
    terse: bool = False,
    timeout: int = 30,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Export RouterOS config over SSH. Sensitive fields are hidden by default."""
    command = "/export"
    if hide_sensitive:
        command += " hide-sensitive"
    if terse:
        command += " terse"
    return ssh(command, timeout=timeout, compact_output=compact_output)


@mcp.tool()
def ssh(command: str, timeout: int = 30, compact_output: bool = True) -> dict[str, Any]:
    """Run a RouterOS command over SSH using MIKROTIK_* credentials."""
    cfg = _config()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    started = time.time()
    try:
        client.connect(
            cfg.host,
            username=cfg.user,
            password=cfg.password or None,
            timeout=cfg.timeout,
            banner_timeout=cfg.timeout,
            auth_timeout=cfg.timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        result = {
            "exit_code": stdout.channel.recv_exit_status(),
            "stdout": out,
            "stderr": err,
            "duration_seconds": round(time.time() - started, 3),
        }
        return compact_command_result(result, enabled=compact_output)
    finally:
        client.close()


if __name__ == "__main__":
    mcp.run()
