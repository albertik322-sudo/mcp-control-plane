from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
import shlex
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paramiko
from dotenv import load_dotenv
from mcp_runtime import configure_stdio, create_mcp_server


ROOT = Path(__file__).resolve().parent
configure_stdio()
load_dotenv(ROOT / ".env", encoding="utf-8-sig")
logging.getLogger("paramiko").setLevel(logging.WARNING)

mcp = create_mcp_server("steamdeck-emulation")


COMMON_FLATPAK_EMULATORS: dict[str, str] = {
    "steam-rom-manager": "com.steamgriddb.steam-rom-manager",
    "srm": "com.steamgriddb.steam-rom-manager",
    "retroarch": "org.libretro.RetroArch",
    "dolphin": "org.DolphinEmu.dolphin-emu",
    "pcsx2": "net.pcsx2.PCSX2",
    "ppsspp": "org.ppsspp.PPSSPP",
    "duckstation": "org.duckstation.DuckStation",
    "rpcs3": "net.rpcs3.RPCS3",
    "citra": "org.citra_emu.citra",
    "melonDS": "net.kuribo64.melonDS",
    "mGBA": "io.mgba.mGBA",
}

ROM_FOLDER_ALIASES: dict[str, str] = {
    "nes": "nes",
    "snes": "snes",
    "n64": "n64",
    "gamecube": "gc",
    "gc": "gc",
    "wii": "wii",
    "gb": "gb",
    "gbc": "gbc",
    "gba": "gba",
    "nds": "nds",
    "3ds": "n3ds",
    "n3ds": "n3ds",
    "ps1": "psx",
    "psx": "psx",
    "ps2": "ps2",
    "ps3": "ps3",
    "psp": "psp",
    "genesis": "genesis",
    "megadrive": "megadrive",
    "dreamcast": "dreamcast",
    "saturn": "saturn",
    "arcade": "arcade",
    "mame": "mame",
}


@dataclass(frozen=True)
class DeckConfig:
    host: str
    port: int
    user: str
    password: str | None
    key_filename: str | None
    sudo_password: str | None
    connect_timeout: float
    local_dropbox: Path
    emulation_root: str | None


def _env(name: str, fallback: str = "") -> str:
    return os.getenv(name, fallback).strip()


def _config() -> DeckConfig:
    host = _env("STEAMDECK_HOST") or _env("DECK_HOST")
    user = _env("STEAMDECK_USER", "deck") or "deck"
    if not host:
        raise RuntimeError("STEAMDECK_HOST must be set in .env")

    key_filename = _env("STEAMDECK_KEY") or _env("STEAMDECK_KEY_FILENAME") or None
    return DeckConfig(
        host=host,
        port=int(_env("STEAMDECK_PORT", "22")),
        user=user,
        password=os.getenv("STEAMDECK_PASSWORD") or None,
        key_filename=key_filename,
        sudo_password=os.getenv("STEAMDECK_SUDO_PASSWORD") or os.getenv("STEAMDECK_PASSWORD") or None,
        connect_timeout=float(_env("STEAMDECK_CONNECT_TIMEOUT", "15")),
        local_dropbox=Path(_env("STEAMDECK_LOCAL_DROPBOX", str(ROOT / "steamdeck_dropbox"))),
        emulation_root=_env("STEAMDECK_EMULATION_ROOT") or None,
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
        password=cfg.password,
        key_filename=cfg.key_filename,
        timeout=cfg.connect_timeout,
        banner_timeout=cfg.connect_timeout,
        auth_timeout=cfg.connect_timeout,
    )
    return client


def _redact(text: str) -> str:
    cfg = _config()
    for secret in (cfg.password, cfg.sudo_password):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _exec(command: str, timeout: int = 60, sudo: bool = False, env: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = _config()
    started = time.time()
    remote_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if env:
        remote_env.update(env)
    prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in remote_env.items()) + " "
    wrapped = prefix + command
    if sudo:
        if not cfg.sudo_password:
            raise RuntimeError("STEAMDECK_SUDO_PASSWORD or STEAMDECK_PASSWORD is required for sudo")
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

    return {
        "command": command,
        "sudo": sudo,
        "exit_code": exit_code,
        "stdout": _redact(out),
        "stderr": _redact(err),
        "duration_seconds": round(time.time() - started, 3),
    }


def _desktop_command(command: str) -> str:
    desktop_env = r"""
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/1000/bus}"
export DISPLAY="${DISPLAY:-:0}"
if [ -z "${XAUTHORITY:-}" ]; then
  XAUTHORITY="$(ps -eww -o args= | sed -n 's/.*Xorg .* -auth \([^ ]*\).*/\1/p' | head -n1)"
  [ -n "$XAUTHORITY" ] && export XAUTHORITY
fi
"""
    return desktop_env + "\n" + command


def _json_exec(script: str, timeout: int = 60) -> dict[str, Any]:
    result = _exec(script, timeout=timeout)
    try:
        result["json"] = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


def _shell_path(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("remote path must be absolute")
    return path


def _home_expr(path: str = "") -> str:
    suffix = "/" + path.strip("/") if path else ""
    return f"$HOME{suffix}"


def _normalize_system(system: str) -> str:
    key = system.strip().lower()
    if not key or any(ch in key for ch in "\r\n/\\"):
        raise ValueError("system must be a folder name or known alias")
    return ROM_FOLDER_ALIASES.get(key, key)


def _default_emulation_roots_command() -> str:
    return r"""
python3 - <<'PY'
import json, os
from pathlib import Path

def safe_exists(path):
    try:
        return path.exists()
    except OSError:
        return False

home = Path.home()
candidates = [
    Path("/run/media/deck/primary/Emulation"),
    Path("/run/media/deck/mmcblk0p1/Emulation"),
    Path("/run/media/mmcblk0p1/Emulation"),
    home / "Emulation",
    home / "emulation",
]
for parent in Path("/run/media").glob("*"):
    try:
        children = list(parent.iterdir())
    except OSError:
        continue
    for media in children:
        candidates.append(media / "Emulation")
seen = []
rows = []
for path in candidates:
    s = str(path)
    if s in seen:
        continue
    seen.append(s)
    rows.append({
        "path": s,
        "exists": safe_exists(path),
        "roms": str(path / "roms"),
        "roms_exists": safe_exists(path / "roms"),
        "bios": str(path / "bios"),
        "bios_exists": safe_exists(path / "bios"),
    })
print(json.dumps(rows, ensure_ascii=False))
PY
"""


def _first_existing_emulation_root(preferred_root: str | None = None) -> str:
    if preferred_root:
        return _shell_path(preferred_root)
    cfg_root = _config().emulation_root
    if cfg_root:
        return _shell_path(cfg_root)
    result = _json_exec(_default_emulation_roots_command(), timeout=30)
    rows = result.get("json") or []
    for row in rows:
        path = str(row.get("path") or "")
        if path.startswith("/run/media/deck/") and row.get("exists") and (row.get("roms_exists") or row.get("bios_exists")):
            return path
    for row in rows:
        if row.get("exists") and (row.get("roms_exists") or row.get("bios_exists")):
            return str(row["path"])
    for row in rows:
        if row.get("path"):
            return str(row["path"])
    return "/home/deck/Emulation"


def _srm_command(root: str | None = None) -> str:
    root_path = _first_existing_emulation_root(root)
    candidates = [
        posixpath.join(root_path, "tools", "Steam-ROM-Manager.AppImage"),
        posixpath.join(root_path, "tools", "Steam ROM Manager.AppImage"),
        posixpath.join(root_path, "tools", "srm", "Steam-ROM-Manager.AppImage"),
        posixpath.join(root_path, "tools", "launchers", "srm", "steamrommanager.sh"),
        _home_expr("Applications/Steam-ROM-Manager.AppImage"),
        _home_expr("Applications/steam-rom-manager.AppImage"),
    ]
    checks = " ".join(shlex.quote(path) for path in candidates)
    return (
        "if command -v steam-rom-manager >/dev/null 2>&1; then printf '%s\\n' steam-rom-manager; "
        "elif flatpak info com.steamgriddb.steam-rom-manager >/dev/null 2>&1; then printf '%s\\n' 'flatpak run com.steamgriddb.steam-rom-manager'; "
        f"else for p in {checks}; do [ -x \"$p\" ] && printf '%s\\n' \"$p\" && exit 0; done; exit 1; fi"
    )


def _run_srm(args: str, root: str | None = None, timeout: int = 300, close_steam: bool = False) -> dict[str, Any]:
    if any(ch in args for ch in "\r\n"):
        raise ValueError("args must be a single command line")
    if close_steam:
        prefix = "pkill -f 'steamwebhelper|steam -gamepadui|/steam' || true; sleep 3; "
    else:
        prefix = ""
    script = f"""
set -u
SRM_CMD="$({ _srm_command(root) })" || {{
  echo "Steam ROM Manager was not found. Install EmuDeck or the SRM Flatpak first." >&2
  exit 127
}}
{prefix}$SRM_CMD {args}
"""
    return _exec(_desktop_command(script), timeout=timeout)


def _sftp_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    current = "/"
    for part in remote_dir.strip("/").split("/"):
        if not part:
            continue
        current = posixpath.join(current, part)
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def _sftp_put_file(sftp: paramiko.SFTPClient, local_path: Path, remote_path: str) -> dict[str, Any]:
    _sftp_mkdirs(sftp, posixpath.dirname(remote_path))
    started = time.time()
    sftp.put(str(local_path), remote_path)
    st = sftp.stat(remote_path)
    return {
        "local_path": str(local_path),
        "remote_path": remote_path,
        "bytes": st.st_size,
        "duration_seconds": round(time.time() - started, 3),
    }


def _iter_local_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    return [p for p in path.rglob("*") if p.is_file()]


@mcp.tool()
def deck_probe() -> dict[str, Any]:
    """Check SSH connectivity and confirm basic SteamOS/Linux identity."""
    script = r"""
set -u
printf 'user=%s\n' "$(whoami)"
printf 'hostname=%s\n' "$(hostname)"
printf 'kernel=%s\n' "$(uname -a)"
printf 'os_release=\n'
cat /etc/os-release 2>/dev/null | sed -n '1,20p'
printf 'steamdeck_hw=\n'
cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null || true
"""
    return _exec(script, timeout=20)


@mcp.tool()
def deck_system_overview() -> dict[str, Any]:
    """Return storage, battery, network, Steam, and failed-service status for the Deck."""
    script = r"""
set -u
printf 'time=%s\n' "$(date -Is)"
printf 'uptime=%s\n' "$(uptime -p || true)"
printf '\n== storage ==\n'
df -hT "$HOME" /run/media/*/* 2>/dev/null | sed -n '1,40p'
printf '\n== memory ==\n'
free -h
printf '\n== battery ==\n'
upower -i "$(upower -e 2>/dev/null | grep BAT | head -n1)" 2>/dev/null | sed -n '1,50p' || true
printf '\n== network ==\n'
ip -br addr | sed -n '1,30p'
printf '\n== steam processes ==\n'
pgrep -a -f 'steam|gamescope|steamwebhelper' | sed -n '1,40p' || true
printf '\n== failed user services ==\n'
systemctl --user --failed --no-pager 2>/dev/null || true
"""
    return _exec(script, timeout=30)


@mcp.tool()
def deck_exec(command: str, timeout: int = 60, sudo: bool = False) -> dict[str, Any]:
    """Run an explicit shell command on the Steam Deck over SSH."""
    if not command.strip():
        raise ValueError("command must not be empty")
    return _exec(command, timeout=max(1, int(timeout)), sudo=sudo)


@mcp.tool()
def discover_emulation_roots() -> dict[str, Any]:
    """Find likely EmuDeck or manual Emulation folders on internal storage and SD cards."""
    return _json_exec(_default_emulation_roots_command(), timeout=30)


@mcp.tool()
def create_emulation_folders(root: str | None = None, systems: list[str] | None = None) -> dict[str, Any]:
    """Create Emulation/bios and Emulation/roms folders for selected systems."""
    root_path = _first_existing_emulation_root(root)
    systems = systems or ["nes", "snes", "n64", "gc", "wii", "gb", "gbc", "gba", "nds", "psx", "ps2", "psp"]
    folders = [posixpath.join(root_path, "bios")]
    folders += [posixpath.join(root_path, "roms", _normalize_system(system)) for system in systems]
    command = "mkdir -p " + " ".join(shlex.quote(folder) for folder in folders)
    result = _exec(command, timeout=30)
    result["root"] = root_path
    result["folders"] = folders
    return result


@mcp.tool()
def list_rom_systems(root: str | None = None) -> dict[str, Any]:
    """List ROM system folders and file counts under Emulation/roms."""
    root_path = _first_existing_emulation_root(root)
    roms = posixpath.join(root_path, "roms")
    script = f"""
python3 - <<'PY'
import json
from pathlib import Path
roms = Path({roms!r})
rows = []
if roms.exists():
    for child in sorted([p for p in roms.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        files = [p for p in child.rglob("*") if p.is_file()]
        rows.append({{"system": child.name, "path": str(child), "files": len(files), "bytes": sum(p.stat().st_size for p in files)}})
print(json.dumps(rows, ensure_ascii=False))
PY
"""
    result = _json_exec(script, timeout=60)
    result["root"] = root_path
    return result


@mcp.tool()
def list_roms(system: str, root: str | None = None, max_count: int = 300) -> dict[str, Any]:
    """List ROM files for one system folder."""
    root_path = _first_existing_emulation_root(root)
    folder = posixpath.join(root_path, "roms", _normalize_system(system))
    script = f"""
python3 - <<'PY'
import json
from pathlib import Path
folder = Path({folder!r})
rows = []
if folder.exists():
    for path in sorted([p for p in folder.rglob("*") if p.is_file()], key=lambda p: str(p).lower())[:{int(max_count)}]:
        st = path.stat()
        rows.append({{"name": path.name, "path": str(path), "bytes": st.st_size, "mtime": st.st_mtime}})
print(json.dumps(rows, ensure_ascii=False))
PY
"""
    result = _json_exec(script, timeout=60)
    result["system"] = _normalize_system(system)
    result["folder"] = folder
    return result


@mcp.tool()
def upload_rom(local_path: str, system: str, root: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Upload a local, user-provided game dump into Emulation/roms/<system> via SFTP."""
    source = Path(local_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(str(source))
    root_path = _first_existing_emulation_root(root)
    system_folder = _normalize_system(system)
    remote_dir = posixpath.join(root_path, "roms", system_folder)
    uploaded: list[dict[str, Any]] = []
    with _client() as client:
        with client.open_sftp() as sftp:
            for file_path in _iter_local_files(source):
                rel = file_path.name if source.is_file() else str(file_path.relative_to(source)).replace("\\", "/")
                remote_path = posixpath.join(remote_dir, rel)
                if not overwrite:
                    try:
                        sftp.stat(remote_path)
                        uploaded.append({"local_path": str(file_path), "remote_path": remote_path, "skipped": "exists"})
                        continue
                    except FileNotFoundError:
                        pass
                uploaded.append(_sftp_put_file(sftp, file_path, remote_path))
    return {"root": root_path, "system": system_folder, "uploaded": uploaded}


@mcp.tool()
def upload_bios(local_path: str, root: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Upload local, user-provided BIOS files into Emulation/bios via SFTP."""
    source = Path(local_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(str(source))
    root_path = _first_existing_emulation_root(root)
    remote_dir = posixpath.join(root_path, "bios")
    uploaded: list[dict[str, Any]] = []
    with _client() as client:
        with client.open_sftp() as sftp:
            for file_path in _iter_local_files(source):
                rel = file_path.name if source.is_file() else str(file_path.relative_to(source)).replace("\\", "/")
                remote_path = posixpath.join(remote_dir, rel)
                if not overwrite:
                    try:
                        sftp.stat(remote_path)
                        uploaded.append({"local_path": str(file_path), "remote_path": remote_path, "skipped": "exists"})
                        continue
                    except FileNotFoundError:
                        pass
                uploaded.append(_sftp_put_file(sftp, file_path, remote_path))
    return {"root": root_path, "uploaded": uploaded}


@mcp.tool()
def download_from_deck(remote_path: str, local_dir: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Download a file or directory from the Deck into a local dropbox folder."""
    remote = _shell_path(remote_path)
    cfg = _config()
    target_root = Path(local_dir).expanduser().resolve() if local_dir else cfg.local_dropbox.resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    downloaded: list[dict[str, Any]] = []
    with _client() as client:
        with client.open_sftp() as sftp:
            st = sftp.stat(remote)
            if stat.S_ISDIR(st.st_mode):
                stack = [remote]
                while stack:
                    current = stack.pop()
                    for item in sftp.listdir_attr(current):
                        child = posixpath.join(current, item.filename)
                        if stat.S_ISDIR(item.st_mode):
                            stack.append(child)
                            continue
                        rel = child.removeprefix(remote).lstrip("/")
                        local_target = target_root / rel
                        local_target.parent.mkdir(parents=True, exist_ok=True)
                        if local_target.exists() and not overwrite:
                            downloaded.append({"remote_path": child, "local_path": str(local_target), "skipped": "exists"})
                            continue
                        sftp.get(child, str(local_target))
                        downloaded.append({"remote_path": child, "local_path": str(local_target), "bytes": local_target.stat().st_size})
            else:
                local_target = target_root / posixpath.basename(remote)
                if local_target.exists() and not overwrite:
                    return {"downloaded": [{"remote_path": remote, "local_path": str(local_target), "skipped": "exists"}]}
                sftp.get(remote, str(local_target))
                downloaded.append({"remote_path": remote, "local_path": str(local_target), "bytes": local_target.stat().st_size})
    return {"downloaded": downloaded}


@mcp.tool()
def flatpak_emulator_status() -> dict[str, Any]:
    """Show installed and available common emulator Flatpaks."""
    script = f"""
python3 - <<'PY'
import json, shutil, subprocess
apps = {COMMON_FLATPAK_EMULATORS!r}
rows = []
if not shutil.which("flatpak"):
    print(json.dumps({{"flatpak": False, "emulators": []}}))
    raise SystemExit
installed = subprocess.run(["flatpak", "list", "--app", "--columns=application"], text=True, capture_output=True).stdout.splitlines()
installed_set = set(x.strip() for x in installed if x.strip())
for name, app_id in apps.items():
    rows.append({{"name": name, "app_id": app_id, "installed": app_id in installed_set}})
print(json.dumps({{"flatpak": True, "emulators": rows}}, ensure_ascii=False))
PY
"""
    return _json_exec(script, timeout=30)


@mcp.tool()
def install_flatpak_emulators(emulators: list[str], assume_yes: bool = True) -> dict[str, Any]:
    """Install selected emulator Flatpaks from Flathub by friendly name or app id."""
    if not emulators:
        raise ValueError("emulators must not be empty")
    app_ids: list[str] = []
    unknown: list[str] = []
    for item in emulators:
        key = item.strip()
        if not key:
            continue
        app_id = COMMON_FLATPAK_EMULATORS.get(key) or COMMON_FLATPAK_EMULATORS.get(key.lower()) or key
        if "." not in app_id:
            unknown.append(item)
            continue
        app_ids.append(app_id)
    if unknown:
        raise ValueError(f"unknown emulator names: {unknown}")
    yes = "-y" if assume_yes else ""
    command = "flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo && "
    command += "flatpak install " + yes + " flathub " + " ".join(shlex.quote(app) for app in app_ids)
    return _exec(command, timeout=1800)


@mcp.tool()
def emudeck_status() -> dict[str, Any]:
    """Inspect likely EmuDeck installation markers, shortcuts, and folders."""
    script = r"""
python3 - <<'PY'
import json
from pathlib import Path
home = Path.home()
paths = [
    home / "Applications",
    home / "Desktop",
    home / "Emulation",
    Path("/run/media/mmcblk0p1/Emulation"),
]
markers = []
for root in paths:
    if not root.exists():
        markers.append({"path": str(root), "exists": False})
        continue
    matches = []
    for pattern in ("*EmuDeck*", "*Steam ROM Manager*", "*Steam-ROM-Manager*", "*SRM*"):
        matches.extend(str(p) for p in root.glob(pattern))
    markers.append({"path": str(root), "exists": True, "matches": sorted(set(matches))[:50]})
print(json.dumps(markers, ensure_ascii=False))
PY
"""
    return _json_exec(script, timeout=30)


@mcp.tool()
def steam_rom_manager_status(root: str | None = None) -> dict[str, Any]:
    """Find Steam ROM Manager and show its CLI help/version output."""
    result = _run_srm("--help", root=root, timeout=60, close_steam=False)
    result["root"] = _first_existing_emulation_root(root)
    return result


@mcp.tool()
def steam_rom_manager_cli(args: str = "list", root: str | None = None, close_steam: bool = False, timeout: int = 300) -> dict[str, Any]:
    """Run Steam ROM Manager CLI arguments such as list, enable, disable, add, remove, or nuke."""
    return _run_srm(args=args, root=root, close_steam=close_steam, timeout=max(30, int(timeout)))


@mcp.tool()
def steam_rom_manager_list(root: str | None = None) -> dict[str, Any]:
    """List Steam ROM Manager parsers through its CLI."""
    return _run_srm("list", root=root, timeout=120, close_steam=False)


@mcp.tool()
def steam_rom_manager_add(parsers: list[str] | None = None, root: str | None = None, close_steam: bool = True) -> dict[str, Any]:
    """Run Steam ROM Manager add. Steam should be closed while shortcuts are written."""
    args = "add"
    if parsers:
        safe = []
        for parser in parsers:
            if any(ch in parser for ch in "\r\n"):
                raise ValueError("parser names must be single-line strings")
            safe.append(shlex.quote(parser))
        args += " " + " ".join(safe)
    return _run_srm(args, root=root, timeout=900, close_steam=close_steam)


@mcp.tool()
def steam_rom_manager_remove(parsers: list[str] | None = None, root: str | None = None, close_steam: bool = True) -> dict[str, Any]:
    """Remove Steam ROM Manager shortcuts for selected parsers or all configured parsers."""
    args = "remove"
    if parsers:
        safe = []
        for parser in parsers:
            if any(ch in parser for ch in "\r\n"):
                raise ValueError("parser names must be single-line strings")
            safe.append(shlex.quote(parser))
        args += " " + " ".join(safe)
    return _run_srm(args, root=root, timeout=900, close_steam=close_steam)


@mcp.tool()
def steam_rom_manager_nuke(root: str | None = None, close_steam: bool = True) -> dict[str, Any]:
    """Remove all Steam ROM Manager-created shortcuts. This is intentionally explicit."""
    return _run_srm("nuke", root=root, timeout=900, close_steam=close_steam)


@mcp.tool()
def open_emudeck_download_page() -> dict[str, Any]:
    """Open the official EmuDeck website in the Deck desktop session for interactive install."""
    return _exec(
        _desktop_command("xdg-open https://www.emudeck.com/"),
        timeout=20,
    )


@mcp.tool()
def launch_desktop_app(app_id_or_command: str, timeout: int = 20) -> dict[str, Any]:
    """Launch a desktop command or Flatpak app in the active Steam Deck desktop session."""
    value = app_id_or_command.strip()
    if not value or any(ch in value for ch in "\r\n"):
        raise ValueError("app_id_or_command must be a single command")
    if "." in value and "/" not in value and " " not in value:
        command = f"flatpak run {shlex.quote(value)}"
    else:
        command = value
    return _exec(_desktop_command(command), timeout=timeout)


@mcp.tool()
def steam_processes() -> dict[str, Any]:
    """List Steam, gamescope, emulator, and Steam ROM Manager processes."""
    return _exec("pgrep -a -f 'steam|gamescope|retroarch|dolphin|pcsx2|ppsspp|duckstation|rpcs3|rom manager|SRM' || true", timeout=20)


@mcp.tool()
def restart_steam_game_mode() -> dict[str, Any]:
    """Restart Steam in Game Mode. This interrupts the current Steam UI session."""
    script = r"""
systemctl --user restart gamescope-session.service 2>/dev/null || {
  pkill -f steam || true
  sleep 2
  steam -gamepadui >/tmp/codex-steam-gamepadui.log 2>&1 &
}
"""
    return _exec(_desktop_command(script), timeout=30)


@mcp.tool()
def notify_deck(title: str, message: str) -> dict[str, Any]:
    """Show a desktop notification on the Deck."""
    safe_title = title.replace("\n", " ").strip()[:80]
    safe_message = message.replace("\n", " ").strip()[:240]
    command = f"notify-send {shlex.quote(safe_title)} {shlex.quote(safe_message)}"
    return _exec(_desktop_command(command), timeout=15)


@mcp.tool()
def scan_bios(root: str | None = None, max_count: int = 500) -> dict[str, Any]:
    """List BIOS files under Emulation/bios without validating copyrighted contents."""
    root_path = _first_existing_emulation_root(root)
    bios = posixpath.join(root_path, "bios")
    script = f"""
python3 - <<'PY'
import json
from pathlib import Path
bios = Path({bios!r})
rows = []
if bios.exists():
    for path in sorted([p for p in bios.rglob("*") if p.is_file()], key=lambda p: str(p).lower())[:{int(max_count)}]:
        st = path.stat()
        rows.append({{"name": path.name, "path": str(path), "bytes": st.st_size}})
print(json.dumps(rows, ensure_ascii=False))
PY
"""
    result = _json_exec(script, timeout=60)
    result["root"] = root_path
    return result


@mcp.tool()
def generate_emulation_report(root: str | None = None) -> dict[str, Any]:
    """Collect a broad emulation setup report: roots, emulators, ROM counts, BIOS count, and processes."""
    roots = discover_emulation_roots()
    status = flatpak_emulator_status()
    emudeck = emudeck_status()
    srm = steam_rom_manager_list(root=root)
    systems = list_rom_systems(root=root)
    bios = scan_bios(root=root, max_count=1000)
    procs = steam_processes()
    return {
        "roots": roots,
        "flatpaks": status,
        "emudeck": emudeck,
        "steam_rom_manager": srm,
        "rom_systems": systems,
        "bios_files": {
            "count": len(bios.get("json") or []),
            "sample": (bios.get("json") or [])[:30],
        },
        "processes": procs,
    }


@mcp.tool()
def write_quickstart_on_deck(root: str | None = None) -> dict[str, Any]:
    """Write a small README in the Deck Emulation folder describing the local folder layout."""
    root_path = _first_existing_emulation_root(root)
    content = f"""# Steam Deck Emulation Folder

Root: {root_path}

- Put legally owned game dumps in `roms/<system>/`.
- Put legally owned BIOS files in `bios/`.
- Use EmuDeck or Steam ROM Manager from Desktop Mode to parse games into Steam.
- Common aliases used by this MCP: ps1 -> psx, gamecube -> gc, 3ds -> n3ds.
"""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    path = posixpath.join(root_path, "CODEX_MCP_QUICKSTART.md")
    command = f"mkdir -p {shlex.quote(root_path)} && base64 -d > {shlex.quote(path)} <<'EOF'\n{encoded}\nEOF"
    result = _exec(command, timeout=30)
    result["path"] = path
    return result


if __name__ == "__main__":
    mcp.run()
