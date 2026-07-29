from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paramiko
from dotenv import load_dotenv
from mcp_runtime import compact_command_result, configure_stdio, create_mcp_server
from ssh_auth import ssh_connect_kwargs


ROOT = Path(__file__).resolve().parent
configure_stdio()
load_dotenv(ROOT / ".env", encoding="utf-8-sig")
logging.getLogger("paramiko").setLevel(logging.WARNING)

mcp = create_mcp_server("ubuntu-server")


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    port: int
    user: str
    password: str | None
    sudo_password: str | None
    connect_timeout: float


def _config() -> RemoteConfig:
    host = os.getenv("REMOTE_HOST", "").strip()
    user = os.getenv("REMOTE_USER", "").strip()
    if not host or not user:
        raise RuntimeError("REMOTE_HOST and REMOTE_USER must be set in .env")

    return RemoteConfig(
        host=host,
        port=int(os.getenv("REMOTE_PORT", "22")),
        user=user,
        password=os.getenv("REMOTE_PASSWORD") or None,
        sudo_password=os.getenv("REMOTE_SUDO_PASSWORD") or os.getenv("REMOTE_PASSWORD") or None,
        connect_timeout=float(os.getenv("REMOTE_CONNECT_TIMEOUT", "15")),
    )


def _client() -> paramiko.SSHClient:
    cfg = _config()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.user,
        **ssh_connect_kwargs(cfg.password),
        timeout=cfg.connect_timeout,
        banner_timeout=cfg.connect_timeout,
        auth_timeout=cfg.connect_timeout,
    )
    return client


def _bash(command: str, cwd: str | None = None) -> str:
    command = f"export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8 PYTHONUTF8=1; {command}"
    if cwd:
        return f"cd {shlex.quote(cwd)} && {command}"
    return command


def _exec(command: str, timeout: int = 60, sudo: bool = False, cwd: str | None = None) -> dict[str, Any]:
    cfg = _config()
    started = time.time()
    wrapped = _bash(command, cwd)

    if sudo:
        if not cfg.sudo_password:
            raise RuntimeError("REMOTE_SUDO_PASSWORD or REMOTE_PASSWORD must be set for sudo commands")
        remote_command = f"sudo -S -p '' bash -lc {shlex.quote(wrapped)}"
    else:
        remote_command = f"bash -lc {shlex.quote(wrapped)}"

    with _client() as client:
        stdin, stdout, stderr = client.exec_command(remote_command, get_pty=False, timeout=timeout)
        if sudo:
            stdin.write(cfg.sudo_password + "\n")
            stdin.flush()
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")

    if sudo and cfg.sudo_password:
        out = out.replace(cfg.sudo_password, "[redacted]")
        err = err.replace(cfg.sudo_password, "[redacted]")

    return {
        "command": command,
        "cwd": cwd,
        "sudo": sudo,
        "exit_code": exit_code,
        "stdout": out,
        "stderr": err,
        "duration_seconds": round(time.time() - started, 3),
    }


def _b64_text(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


@mcp.tool()
def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int = 60,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Run a bash command on the Ubuntu server as the SSH user."""
    return compact_command_result(
        _exec(command=command, cwd=cwd, timeout=timeout, sudo=False),
        enabled=compact_output,
    )


@mcp.tool()
def run_sudo_command(
    command: str,
    cwd: str | None = None,
    timeout: int = 60,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Run a bash command on the Ubuntu server through sudo."""
    return compact_command_result(
        _exec(command=command, cwd=cwd, timeout=timeout, sudo=True),
        enabled=compact_output,
    )


@mcp.tool()
def run_script(
    script: str,
    sudo: bool = False,
    timeout: int = 120,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Upload a temporary bash script to the Ubuntu server and execute it."""
    remote_path = f"/tmp/codex-mcp-{int(time.time() * 1000)}.sh"
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    create = f"base64 -d > {shlex.quote(remote_path)} <<'EOF'\n{encoded}\nEOF\nchmod 700 {shlex.quote(remote_path)}"
    _exec(create, timeout=30)
    try:
        return compact_command_result(
            _exec(shlex.quote(remote_path), timeout=timeout, sudo=sudo),
            enabled=compact_output,
        )
    finally:
        _exec(f"rm -f {shlex.quote(remote_path)}", timeout=30)


@mcp.tool()
def run_python_script(
    script: str,
    sudo: bool = False,
    timeout: int = 120,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Upload a temporary UTF-8 Python script and execute it with python3.

    Prefer this over shell heredocs for multi-line edits. The payload is transferred
    as base64, so quotes, newlines, emoji, and backslashes survive unchanged.
    """
    remote_path = f"/tmp/codex-mcp-{int(time.time() * 1000)}.py"
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    create = f"base64 -d > {shlex.quote(remote_path)} <<'EOF'\n{encoded}\nEOF\nchmod 700 {shlex.quote(remote_path)}"
    _exec(create, timeout=30)
    try:
        return compact_command_result(
            _exec(f"python3 {shlex.quote(remote_path)}", timeout=timeout, sudo=sudo),
            enabled=compact_output,
        )
    finally:
        _exec(f"rm -f {shlex.quote(remote_path)}", timeout=30)


@mcp.tool()
def python_edit_file(
    path: str,
    script: str,
    sudo: bool = False,
    backup: bool = True,
    timeout: int = 120,
) -> dict[str, Any]:
    """Safely edit one remote UTF-8 file with a Python transform script.

    The transform receives variables:
    - path: pathlib.Path for the target file
    - text: current file text

    It must set either:
    - new_text: replacement text, or
    - text: modified text

    A timestamped sibling backup is created by default. The transform is base64
    uploaded and executed remotely, avoiding nested quote/newline problems.
    """
    if not path.startswith("/"):
        raise ValueError("path must be absolute")
    driver = f"""
from __future__ import annotations
from pathlib import Path
import time

path = Path({path!r})
text = path.read_text(encoding="utf-8")
old_text = text
namespace = {{"Path": Path, "path": path, "text": text}}
exec({script!r}, namespace)
new_text = namespace.get("new_text", namespace.get("text", text))
if not isinstance(new_text, str):
    raise TypeError("transform must leave text/new_text as str")
if new_text != old_text:
    if {bool(backup)!r}:
        backup_path = path.with_name(path.name + ".bak-codex-" + time.strftime("%Y%m%d-%H%M%S"))
        backup_path.write_text(old_text, encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
print("changed=" + str(new_text != old_text).lower())
print("path=" + str(path))
"""
    return run_python_script(driver, sudo=sudo, timeout=timeout)


@mcp.tool()
def apply_unified_patch_remote(patch: str, sudo: bool = False, cwd: str = "/", timeout: int = 120) -> dict[str, Any]:
    """Apply a unified diff on the remote server without shell quoting issues.

    The patch is transferred as base64 to a temporary file and applied with
    `patch -p0`. Use absolute paths in the diff, or set cwd for relative paths.
    """
    encoded = base64.b64encode(patch.encode("utf-8")).decode("ascii")
    remote_path = f"/tmp/codex-mcp-patch-{int(time.time() * 1000)}.diff"
    create = f"base64 -d > {shlex.quote(remote_path)} <<'EOF'\n{encoded}\nEOF"
    _exec(create, timeout=30)
    try:
        cmd = f"patch -p0 --batch --forward < {shlex.quote(remote_path)}"
        return _exec(cmd, cwd=cwd, timeout=timeout, sudo=sudo)
    finally:
        _exec(f"rm -f {shlex.quote(remote_path)}", timeout=30)


@mcp.tool()
def write_file(path: str, content: str, sudo: bool = False, mode: str | None = None) -> dict[str, Any]:
    """Write UTF-8 text to a remote file. Use sudo for root-owned paths."""
    if not path.startswith("/"):
        raise ValueError("path must be absolute")

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    tmp_path = f"/tmp/codex-mcp-write-{int(time.time() * 1000)}"
    quoted_tmp = shlex.quote(tmp_path)
    quoted_path = shlex.quote(path)
    parent = shlex.quote(posixpath.dirname(path))

    _exec(f"base64 -d > {quoted_tmp} <<'EOF'\n{encoded}\nEOF", timeout=30)
    command = f"mkdir -p {parent} && mv {quoted_tmp} {quoted_path}"
    if mode:
        command += f" && chmod {shlex.quote(mode)} {quoted_path}"
    return _exec(command, timeout=30, sudo=sudo)


@mcp.tool()
def read_file(path: str, sudo: bool = False, max_bytes: int = 131072) -> dict[str, Any]:
    """Read a remote file as UTF-8 text with a byte limit."""
    if not path.startswith("/"):
        raise ValueError("path must be absolute")
    command = (
        f"python3 - <<'PY'\n"
        f"from pathlib import Path\n"
        f"p=Path({path!r})\n"
        f"data=p.read_bytes()[:{int(max_bytes)}]\n"
        f"print(data.decode('utf-8', errors='replace'))\n"
        f"PY"
    )
    return _exec(command, timeout=30, sudo=sudo)


@mcp.tool()
def read_file_base64(path: str, sudo: bool = False, max_bytes: int = 10485760) -> dict[str, Any]:
    """Read a remote file as base64 for binary-safe transfer."""
    if not path.startswith("/"):
        raise ValueError("path must be absolute")
    command = (
        f"python3 - <<'PY'\n"
        f"import base64\n"
        f"from pathlib import Path\n"
        f"p=Path({path!r})\n"
        f"data=p.read_bytes()[:{int(max_bytes)}]\n"
        f"print(base64.b64encode(data).decode('ascii'))\n"
        f"PY"
    )
    result = _exec(command, timeout=60, sudo=sudo)
    result["path"] = path
    result["encoding"] = "base64"
    return result


@mcp.tool()
def write_file_base64(path: str, content_base64: str, sudo: bool = False, mode: str | None = None) -> dict[str, Any]:
    """Write binary content from base64 to a remote file."""
    if not path.startswith("/"):
        raise ValueError("path must be absolute")
    tmp_path = f"/tmp/codex-mcp-binary-{int(time.time() * 1000)}"
    create = f"base64 -d > {shlex.quote(tmp_path)} <<'EOF'\n{content_base64}\nEOF"
    _exec(create, timeout=60)
    command = f"mkdir -p {shlex.quote(posixpath.dirname(path))} && mv {shlex.quote(tmp_path)} {shlex.quote(path)}"
    if mode:
        command += f" && chmod {shlex.quote(mode)} {shlex.quote(path)}"
    return _exec(command, timeout=60, sudo=sudo)


@mcp.tool()
def list_dir(path: str = ".", sudo: bool = False) -> dict[str, Any]:
    """List a remote directory with file metadata."""
    command = f"python3 - <<'PY'\nimport json, os, stat\nfrom pathlib import Path\np=Path({path!r})\nrows=[]\nfor x in sorted(p.iterdir(), key=lambda v: v.name.lower()):\n    s=x.lstat()\n    rows.append({{'name':x.name,'path':str(x),'type':'dir' if x.is_dir() else 'file' if x.is_file() else 'other','size':s.st_size,'mode':oct(s.st_mode & 0o7777),'mtime':s.st_mtime}})\nprint(json.dumps(rows, ensure_ascii=False))\nPY"
    return _exec(command, timeout=30, sudo=sudo)


@mcp.tool()
def stat_path(path: str, sudo: bool = False) -> dict[str, Any]:
    """Return stat metadata for a remote path."""
    command = f"stat -c '%n|%F|%s|%U|%G|%a|%y' {shlex.quote(path)}"
    return _exec(command, timeout=15, sudo=sudo)


@mcp.tool()
def tail_file(
    path: str,
    lines: int = 100,
    sudo: bool = False,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Tail a remote text file."""
    return compact_command_result(
        _exec(f"tail -n {int(lines)} {shlex.quote(path)}", timeout=30, sudo=sudo),
        filter_name="log",
        enabled=compact_output,
    )


@mcp.tool()
def grep_files(
    pattern: str,
    path: str,
    sudo: bool = False,
    max_count: int = 200,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Search remote files using rg when available, falling back to grep."""
    cmd = (
        f"if command -v rg >/dev/null 2>&1; then "
        f"rg -n --hidden --no-ignore -m 20 {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -n {int(max_count)}; "
        f"else grep -RIn {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -n {int(max_count)}; fi"
    )
    return compact_command_result(
        _exec(cmd, timeout=60, sudo=sudo),
        filter_name="grep",
        enabled=compact_output,
    )


@mcp.tool()
def system_overview() -> dict[str, Any]:
    """Collect a compact Ubuntu server health overview."""
    script = r"""
set -u
printf 'hostname=%s\n' "$(hostname)"
printf 'now=%s\n' "$(date -Is)"
printf 'uptime=%s\n' "$(uptime -p)"
printf 'kernel=%s\n' "$(uname -r)"
printf 'loadavg=%s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"
free -h
df -hT | sed -n '1,40p'
systemctl --failed --no-pager || true
"""
    return _exec(script, timeout=30, sudo=True)


@mcp.tool()
def hardware_overview() -> dict[str, Any]:
    """Collect CPU, memory, block devices, SMART availability, and sensor hints."""
    script = r"""
set -u
printf 'hostname=%s\n' "$(hostname)"
printf 'machine=%s\n' "$(uname -m)"
printf '\nCPU\n'
lscpu | sed -n '1,35p' || true
printf '\nMEMORY\n'
free -h || true
printf '\nBLOCK DEVICES\n'
lsblk -o NAME,TYPE,SIZE,FSTYPE,FSUSE%,MOUNTPOINTS,MODEL,SERIAL | sed -n '1,80p' || true
printf '\nNVME\n'
if command -v nvme >/dev/null 2>&1; then nvme list || true; else echo 'nvme cli not installed'; fi
printf '\nSMART\n'
if command -v smartctl >/dev/null 2>&1; then
  for d in /dev/sd? /dev/nvme?n?; do [ -e "$d" ] && smartctl -H "$d" 2>/dev/null | sed "s#^#$d #"; done
else
  echo 'smartmontools not installed'
fi
printf '\nSENSORS\n'
if command -v sensors >/dev/null 2>&1; then sensors; else echo 'lm-sensors not installed'; fi
"""
    return _exec(script, timeout=60, sudo=True)


@mcp.tool()
def server_health() -> dict[str, Any]:
    """Return a practical health bundle for the Ubuntu server."""
    return {
        "overview": system_overview(),
        "failed_units": _exec("systemctl --failed --no-pager", timeout=30, sudo=True),
        "disk_pressure": _exec("df -hT | awk 'NR==1 || int($6) >= 80 {print}'", timeout=30, sudo=True),
        "memory_pressure": _exec("free -h && printf '\\nOOM events\\n'; journalctl -k --since '24 hours ago' --no-pager | grep -Ei 'out of memory|oom-killer' | tail -n 50 || true", timeout=60, sudo=True),
        "recent_errors": journal(since="2 hours ago", lines=120, priority="warning"),
    }


@mcp.tool()
def service_status(services: list[str] | None = None) -> dict[str, Any]:
    """Return systemd active/enabled states for services."""
    services = services or ["apache2", "postgresql", "redis-server", "fail2ban", "rsyslog", "codex-control"]
    quoted = " ".join(shlex.quote(s) for s in services)
    cmd = f"for s in {quoted}; do printf '%s\\t%s\\t%s\\n' \"$s\" \"$(systemctl is-active \"$s\" 2>/dev/null || echo missing)\" \"$(systemctl is-enabled \"$s\" 2>/dev/null || echo missing)\"; done"
    return _exec(cmd, timeout=30, sudo=True)


@mcp.tool()
def service_control(service: str, action: str, timeout: int = 60) -> dict[str, Any]:
    """Run systemctl action for a service: status, restart, reload, start, stop, enable, disable."""
    allowed = {"status", "restart", "reload", "start", "stop", "enable", "disable"}
    if action not in allowed:
        raise ValueError(f"action must be one of {sorted(allowed)}")
    return _exec(f"systemctl {action} {shlex.quote(service)} --no-pager", timeout=timeout, sudo=True)


@mcp.tool()
def journal(
    unit: str | None = None,
    since: str = "1 hour ago",
    lines: int = 200,
    priority: str | None = None,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Read systemd journal logs."""
    cmd = "journalctl --no-pager"
    if unit:
        cmd += f" -u {shlex.quote(unit)}"
    if since:
        cmd += f" --since {shlex.quote(since)}"
    if priority:
        cmd += f" -p {shlex.quote(priority)}"
    cmd += f" | tail -n {int(lines)}"
    return compact_command_result(
        _exec(cmd, timeout=60, sudo=True),
        filter_name="log",
        enabled=compact_output,
    )


@mcp.tool()
def boot_errors(lines: int = 200, compact_output: bool = True) -> dict[str, Any]:
    """Return warning-or-worse journal entries from the current boot."""
    result = _exec(
        f"journalctl -b -p warning --no-pager | tail -n {max(1, min(int(lines), 1000))}",
        timeout=60,
        sudo=True,
    )
    return compact_command_result(result, filter_name="log", enabled=compact_output)


@mcp.tool()
def login_audit(lines: int = 120) -> dict[str, Any]:
    """Return recent SSH/authentication activity and current logins."""
    script = f"""
printf 'WHO\\n'
who || true
printf '\\nLAST LOGINS\\n'
last -a | sed -n '1,{max(1, min(int(lines), 500))}p' || true
printf '\\nAUTH LOG\\n'
if [ -f /var/log/auth.log ]; then
  grep -Ei 'accepted|failed|invalid|sudo|session' /var/log/auth.log | tail -n {max(1, min(int(lines), 500))} || true
else
  journalctl _COMM=sshd --since '24 hours ago' --no-pager | tail -n {max(1, min(int(lines), 500))} || true
fi
"""
    return _exec(script, timeout=60, sudo=True)


@mcp.tool()
def apt_summary() -> dict[str, Any]:
    """Show upgradable packages and simulated autoremove."""
    cmd = "printf 'upgradable:\\n'; apt list --upgradable 2>/dev/null | sed -n '1,120p'; printf '\\nautoremove_simulation:\\n'; apt-get -s autoremove --purge | sed -n '1,180p'"
    return _exec(cmd, timeout=60, sudo=True)


@mcp.tool()
def apt_maintenance(upgrade: bool = False, autoremove: bool = False, autoclean: bool = True) -> dict[str, Any]:
    """Run apt update plus optional upgrade/autoremove/autoclean."""
    parts = ["export DEBIAN_FRONTEND=noninteractive", "apt-get update"]
    if upgrade:
        parts.append("apt-get -y upgrade")
    if autoremove:
        parts.append("apt-get -y autoremove --purge")
    if autoclean:
        parts.append("apt-get -y autoclean")
    return _exec(" && ".join(parts), timeout=900, sudo=True)


@mcp.tool()
def apt_security_summary() -> dict[str, Any]:
    """Show pending security updates when update metadata is available."""
    script = r"""
set -u
apt-get update -qq >/dev/null || true
printf 'UPGRADABLE\n'
apt list --upgradable 2>/dev/null | sed -n '1,200p'
printf '\nSECURITY CANDIDATES\n'
if command -v unattended-upgrade >/dev/null 2>&1; then
  unattended-upgrade --dry-run --debug 2>&1 | grep -Ei 'security|upgrade|package' | sed -n '1,220p' || true
else
  apt list --upgradable 2>/dev/null | grep -Ei 'security|ubuntu[/-].*-security' | sed -n '1,220p' || true
fi
"""
    return _exec(script, timeout=180, sudo=True)


@mcp.tool()
def ports_list() -> dict[str, Any]:
    """List listening TCP/UDP ports."""
    return _exec("ss -tulpen", timeout=30, sudo=True)


@mcp.tool()
def ufw_status() -> dict[str, Any]:
    """Return UFW firewall status."""
    return _exec("ufw status verbose", timeout=30, sudo=True)


@mcp.tool()
def security_audit() -> dict[str, Any]:
    """Run a read-only Ubuntu hardening audit with actionable findings."""
    script = r"""
set -u
printf 'FIREWALL\n'
ufw status verbose || true
printf '\nLISTENING PORTS\n'
ss -tulpen || true
printf '\nFAILED SERVICES\n'
systemctl --failed --no-pager || true
printf '\nSSH EFFECTIVE CONFIG\n'
sshd -T 2>/dev/null | grep -Ei '^(permitrootlogin|passwordauthentication|pubkeyauthentication|challengeresponseauthentication|kbdinteractiveauthentication|x11forwarding|allowusers|allowgroups|port) ' || true
printf '\nSUDOERS NOPASSWD\n'
grep -RIn 'NOPASSWD' /etc/sudoers /etc/sudoers.d 2>/dev/null || true
printf '\nWORLD WRITABLE PATHS (top 80)\n'
find /etc /opt /var/www -xdev -type f -perm -0002 -print 2>/dev/null | sed -n '1,80p' || true
printf '\nFAIL2BAN\n'
if command -v fail2ban-client >/dev/null 2>&1; then fail2ban-client status || true; else echo 'fail2ban-client not installed'; fi
"""
    result = _exec(script, timeout=120, sudo=True)
    findings: list[str] = []
    text = result.get("stdout", "").lower()
    if "status: inactive" in text:
        findings.append("UFW appears inactive.")
    if "passwordauthentication yes" in text:
        findings.append("SSH password authentication is enabled.")
    if "permitrootlogin yes" in text or "permitrootlogin prohibit-password" in text:
        findings.append("SSH root login is not fully disabled.")
    if "nopasswd" in text:
        findings.append("NOPASSWD sudo entries exist; verify they are intentional.")
    result["findings"] = findings
    return result


@mcp.tool()
def process_list(limit: int = 80) -> dict[str, Any]:
    """Return top processes by CPU and memory."""
    cmd = f"ps aux --sort=-%cpu | head -n {int(limit)}; printf '\\nMEM\\n'; ps aux --sort=-%mem | head -n {int(limit)}"
    return _exec(cmd, timeout=30, sudo=True)


@mcp.tool()
def backup_file(path: str, backup_dir: str = "/root/codex-backups") -> dict[str, Any]:
    """Copy a remote file to a timestamped root backup directory."""
    stamp = int(time.time())
    base = posixpath.basename(path.rstrip("/"))
    dest = posixpath.join(backup_dir, f"{base}.{stamp}.bak")
    cmd = f"mkdir -p {shlex.quote(backup_dir)} && cp -a {shlex.quote(path)} {shlex.quote(dest)} && ls -lh {shlex.quote(dest)}"
    return _exec(cmd, timeout=60, sudo=True)


@mcp.tool()
def timer_status() -> dict[str, Any]:
    """List systemd timers and recently missed/failed timer activity."""
    script = r"""
systemctl list-timers --all --no-pager
printf '\nFAILED TIMER-RELATED UNITS\n'
systemctl --failed --no-pager | grep -Ei 'timer|service' || true
"""
    return _exec(script, timeout=30, sudo=True)


@mcp.tool()
def nextcloud_occ(args: str, nextcloud_dir: str = "/var/www/nextcloud", timeout: int = 120) -> dict[str, Any]:
    """Run a Nextcloud occ command as www-data, for example: status or app:list."""
    command = f"sudo -u www-data php occ {args}"
    return _exec(command=command, cwd=nextcloud_dir, timeout=timeout, sudo=True)


@mcp.tool()
def nextcloud_status() -> dict[str, Any]:
    """Return Nextcloud status and setup check warnings."""
    cmd = "sudo -u www-data php occ status && sudo -u www-data php occ setupchecks | sed -n '1,260p'"
    return _exec(command=cmd, cwd="/var/www/nextcloud", timeout=180, sudo=True)


@mcp.tool()
def nextcloud_log(lines: int = 100, compact_output: bool = True) -> dict[str, Any]:
    """Tail the configured Nextcloud log via occ."""
    result = _exec(
        command=f"sudo -u www-data php occ log:tail {int(lines)}",
        cwd="/var/www/nextcloud",
        timeout=60,
        sudo=True,
    )
    return compact_command_result(result, filter_name="log", enabled=compact_output)


@mcp.tool()
def postgres_query(database: str, query: str, timeout: int = 60) -> dict[str, Any]:
    """Run a PostgreSQL query as postgres."""
    encoded = _b64_text(query)
    cmd = f"base64 -d <<'EOF' | sudo -u postgres psql -d {shlex.quote(database)}\n{encoded}\nEOF"
    return _exec(cmd, timeout=timeout, sudo=True)


if __name__ == "__main__":
    mcp.run(transport="stdio")
