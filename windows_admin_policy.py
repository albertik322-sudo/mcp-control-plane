from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
ADMIN_ROOT = ROOT / ".runtime" / "windows_admin"
INBOX = ADMIN_ROOT / "inbox"
OUTBOX = ADMIN_ROOT / "outbox"
PROCESSED = ADMIN_ROOT / "processed"
LOGS = ADMIN_ROOT / "logs"
AUDIT_LOG = LOGS / "audit.jsonl"
TASK_NAME = "CodexWindowsAdminBroker"

REQUEST_TTL_SECONDS = 300
MAX_OUTPUT_CHARS = 40_000

SERVICE_RE = re.compile(r"^[A-Za-z0-9_. -]{1,120}$")
RECOVERY_ROOT_RE = re.compile(r"^[A-Za-z]:\\CodexRecovery(?:\\[A-Za-z0-9_. -]{1,80})*$")
VOLUME_LABEL_RE = re.compile(r"^[A-Za-z0-9_. -]{1,32}$")


@dataclass(frozen=True)
class RenderedAction:
    action: str
    command: str
    timeout: int
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "command": self.command,
            "timeout": self.timeout,
            "summary": self.summary,
        }


def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def clamp_timeout(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def require_keys(args: dict[str, Any], keys: set[str]) -> None:
    extra = set(args) - keys
    if extra:
        raise ValueError(f"unsupported argument(s): {sorted(extra)}")


def service_control(args: dict[str, Any], timeout: Any = None) -> RenderedAction:
    require_keys(args, {"name", "action"})
    name = str(args.get("name", "")).strip()
    action = str(args.get("action", "")).strip().lower()
    if not SERVICE_RE.fullmatch(name):
        raise ValueError("service name contains unsupported characters")
    if action not in {"start", "stop", "restart"}:
        raise ValueError("service action must be start, stop, or restart")
    verb = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}[action]
    command = (
        f"{verb} -Name {ps_quote(name)} -ErrorAction Stop; "
        f"Get-Service -Name {ps_quote(name)} | Select-Object Name,Status,StartType | ConvertTo-Json -Depth 3"
    )
    return RenderedAction("service_control", command, clamp_timeout(timeout, 60, 300), f"{action} Windows service {name}")


def persistent_route_add(args: dict[str, Any], timeout: Any = None) -> RenderedAction:
    require_keys(args, {"destination", "mask", "gateway", "interface", "metric"})
    destination = str(ipaddress.IPv4Address(str(args["destination"])))
    mask = str(ipaddress.IPv4Address(str(args["mask"])))
    gateway = str(ipaddress.IPv4Address(str(args["gateway"])))
    interface = int(args["interface"])
    metric = int(args["metric"])
    if interface < 1 or interface > 9999:
        raise ValueError("interface must be 1..9999")
    if metric < 1 or metric > 9999:
        raise ValueError("metric must be 1..9999")
    command = (
        f"route -p add {destination} mask {mask} {gateway} IF {interface} metric {metric}; "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
        "route print"
    )
    return RenderedAction("persistent_route_add", command, clamp_timeout(timeout, 30, 120), f"add persistent route {destination}/{mask}")


def persistent_route_delete(args: dict[str, Any], timeout: Any = None) -> RenderedAction:
    require_keys(args, {"destination"})
    destination = str(ipaddress.IPv4Address(str(args["destination"])))
    command = f"route -p delete {destination}; if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}; route print"
    return RenderedAction("persistent_route_delete", command, clamp_timeout(timeout, 30, 120), f"delete persistent route {destination}")


def wake_device(args: dict[str, Any], timeout: Any = None, *, enable: bool) -> RenderedAction:
    require_keys(args, {"device"})
    device = str(args.get("device", "")).strip()
    if not device or len(device) > 240 or any(ch in device for ch in "\r\n\x00"):
        raise ValueError("device must be a single wake-programmable device name")
    verb = "/deviceenablewake" if enable else "/devicedisablewake"
    action = "enable_wake_device" if enable else "disable_wake_device"
    command = (
        "$device = " + ps_quote(device) + "; "
        "$allowed = @(powercfg /devicequery wake_programmable); "
        "if ($allowed -notcontains $device) { throw 'device is not wake-programmable or was not found' }; "
        f"powercfg {verb} $device; "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
        "powercfg /devicequery wake_armed"
    )
    summary = ("enable" if enable else "disable") + f" wake for device {device}"
    return RenderedAction(action, command, clamp_timeout(timeout, 20, 60), summary)


def fixed_action(action: str, command: str, summary: str, default_timeout: int, max_timeout: int):
    def render(args: dict[str, Any], timeout: Any = None) -> RenderedAction:
        require_keys(args, set())
        return RenderedAction(action, command, clamp_timeout(timeout, default_timeout, max_timeout), summary)

    return render


def removable_disk_image(args: dict[str, Any], timeout: Any = None) -> RenderedAction:
    require_keys(args, {"disk_index", "output_root", "chunk_mb"})
    disk_index = int(args["disk_index"])
    if disk_index < 0 or disk_index > 128:
        raise ValueError("disk_index must be 0..128")
    output_root = str(args.get("output_root") or "D:\\CodexRecovery").strip()
    if not RECOVERY_ROOT_RE.fullmatch(output_root):
        raise ValueError("output_root must be an absolute X:\\CodexRecovery path")
    chunk_mb = int(args.get("chunk_mb") or 4)
    if chunk_mb < 1 or chunk_mb > 32:
        raise ValueError("chunk_mb must be 1..32")
    command = rf"""
$ErrorActionPreference = 'Stop'
$diskIndex = {disk_index}
$outputRoot = {ps_quote(output_root)}
$chunkBytes = {chunk_mb} * 1MB
$disk = Get-CimInstance Win32_DiskDrive | Where-Object {{ $_.Index -eq $diskIndex }}
if (-not $disk) {{ throw "disk index $diskIndex was not found" }}
$isRemovable = ($disk.InterfaceType -eq 'USB') -or ($disk.MediaType -match 'Removable')
if (-not $isRemovable) {{ throw "refusing to image non-removable disk $diskIndex ($($disk.Model), $($disk.MediaType), $($disk.InterfaceType))" }}
if ([uint64]$disk.Size -lt 1MB -or [uint64]$disk.Size -gt 512GB) {{ throw "refusing disk size $($disk.Size)" }}
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$safeModel = (($disk.Model -replace '[^A-Za-z0-9_.-]+','_').Trim('_'))
if (-not $safeModel) {{ $safeModel = 'removable' }}
$imagePath = Join-Path $outputRoot ("disk$diskIndex-$safeModel-$stamp.img")
$logPath = Join-Path $outputRoot ("disk$diskIndex-$safeModel-$stamp.log.jsonl")
$rawPath = "\\.\PhysicalDrive$diskIndex"
$buffer = New-Object byte[] $chunkBytes
$zero = New-Object byte[] 512
$total = [uint64]$disk.Size
$copied = [uint64]0
$errors = 0
$source = [System.IO.File]::Open($rawPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
$dest = [System.IO.File]::Open($imagePath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
try {{
  while ($copied -lt $total) {{
    $remaining = $total - $copied
    $want = [int][Math]::Min([uint64]$buffer.Length, $remaining)
    try {{
      $read = $source.Read($buffer, 0, $want)
      if ($read -le 0) {{ break }}
      $dest.Write($buffer, 0, $read)
      $copied += [uint64]$read
    }} catch {{
      $chunkStart = $copied
      $chunkEnd = [uint64][Math]::Min($chunkStart + [uint64]$want, $total)
      while ($copied -lt $chunkEnd) {{
        $sectorWant = [int][Math]::Min([uint64]512, $chunkEnd - $copied)
        try {{
          $source.Seek([int64]$copied, [System.IO.SeekOrigin]::Begin) | Out-Null
          $sector = New-Object byte[] $sectorWant
          $read = $source.Read($sector, 0, $sectorWant)
          if ($read -gt 0) {{
            $dest.Write($sector, 0, $read)
            $copied += [uint64]$read
          }} else {{
            $dest.Write($zero, 0, $sectorWant)
            $copied += [uint64]$sectorWant
            $errors++
          }}
        }} catch {{
          $dest.Write($zero, 0, $sectorWant)
          (@{{ offset=$copied; bytes=$sectorWant; error=$_.Exception.Message }} | ConvertTo-Json -Compress) | Add-Content -Path $logPath -Encoding UTF8
          $copied += [uint64]$sectorWant
          $errors++
        }}
      }}
      $source.Seek([int64]$copied, [System.IO.SeekOrigin]::Begin) | Out-Null
    }}
  }}
}} finally {{
  $dest.Flush()
  $dest.Close()
  $source.Close()
}}
[pscustomobject]@{{
  disk_index=$diskIndex
  model=$disk.Model
  serial=$disk.SerialNumber
  size=$total
  bytes_copied=$copied
  read_errors=$errors
  image_path=$imagePath
  log_path=$logPath
}} | ConvertTo-Json -Depth 4
""".strip()
    summary = f"create read-only image of removable disk {disk_index} under {output_root}"
    return RenderedAction("removable_disk_image", command, clamp_timeout(timeout, 7200, 14400), summary)


def removable_disk_probe(args: dict[str, Any], timeout: Any = None) -> RenderedAction:
    require_keys(args, {"disk_index"})
    disk_index = int(args["disk_index"])
    if disk_index < 0 or disk_index > 128:
        raise ValueError("disk_index must be 0..128")
    command = rf"""
$ErrorActionPreference = 'Stop'
$diskIndex = {disk_index}
$disk = Get-CimInstance Win32_DiskDrive | Where-Object {{ $_.Index -eq $diskIndex }}
if (-not $disk) {{ throw "disk index $diskIndex was not found" }}
$isRemovable = ($disk.InterfaceType -eq 'USB') -or ($disk.MediaType -match 'Removable')
if (-not $isRemovable) {{ throw "refusing to probe non-removable disk $diskIndex ($($disk.Model), $($disk.MediaType), $($disk.InterfaceType))" }}
$total = [uint64]$disk.Size
if ($total -lt 1MB -or $total -gt 512GB) {{ throw "refusing disk size $($disk.Size)" }}
$rawPath = "\\.\PhysicalDrive$diskIndex"
$offsets = @(
  [uint64]0,
  [uint64]512,
  [uint64]1MB,
  [uint64]64MB,
  [uint64]1GB,
  [uint64][Math]::Max(0, [int64]($total / 2)),
  [uint64][Math]::Max(0, [int64]($total - 1MB))
) | Sort-Object -Unique
$source = [System.IO.File]::Open($rawPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
$results = @()
try {{
  foreach ($offset in $offsets) {{
    $buffer = New-Object byte[] 512
    try {{
      $source.Seek([int64]$offset, [System.IO.SeekOrigin]::Begin) | Out-Null
      $read = $source.Read($buffer, 0, $buffer.Length)
      $results += [pscustomobject]@{{ offset=$offset; ok=$true; bytes_read=$read; first16=(($buffer[0..15] | ForEach-Object {{ $_.ToString('X2') }}) -join ' ') }}
    }} catch {{
      $results += [pscustomobject]@{{ offset=$offset; ok=$false; error=$_.Exception.Message }}
    }}
  }}
}} finally {{
  $source.Close()
}}
[pscustomobject]@{{
  disk_index=$diskIndex
  model=$disk.Model
  serial=$disk.SerialNumber
  size=$total
  results=$results
}} | ConvertTo-Json -Depth 5
""".strip()
    return RenderedAction("removable_disk_probe", command, clamp_timeout(timeout, 30, 120), f"probe readable sectors on removable disk {disk_index}")


def removable_disk_reinitialize(args: dict[str, Any], timeout: Any = None) -> RenderedAction:
    require_keys(args, {"disk_index", "filesystem", "label", "drive_letter"})
    disk_index = int(args["disk_index"])
    if disk_index < 0 or disk_index > 128:
        raise ValueError("disk_index must be 0..128")
    filesystem = str(args.get("filesystem") or "exfat").strip().lower()
    if filesystem not in {"exfat", "fat32", "ntfs"}:
        raise ValueError("filesystem must be exfat, fat32, or ntfs")
    label = str(args.get("label") or "SDCARD").strip()
    if not VOLUME_LABEL_RE.fullmatch(label):
        raise ValueError("label contains unsupported characters")
    drive_letter = str(args.get("drive_letter") or "E").strip().upper().rstrip(":")
    if not re.fullmatch(r"[D-Z]", drive_letter):
        raise ValueError("drive_letter must be D..Z")
    diskpart_lines = [
        f"select disk {disk_index}",
        "attributes disk clear readonly",
        "clean",
        "convert mbr",
        "create partition primary",
        f'format fs={filesystem} quick label="{label}"',
        f"assign letter={drive_letter}",
        "exit",
    ]
    diskpart_script = "\r\n".join(diskpart_lines) + "\r\n"
    command = rf"""
$ErrorActionPreference = 'Stop'
$diskIndex = {disk_index}
$disk = Get-CimInstance Win32_DiskDrive | Where-Object {{ $_.Index -eq $diskIndex }}
if (-not $disk) {{ throw "disk index $diskIndex was not found" }}
$isRemovable = ($disk.InterfaceType -eq 'USB') -or ($disk.MediaType -match 'Removable')
if (-not $isRemovable) {{ throw "refusing to reinitialize non-removable disk $diskIndex ($($disk.Model), $($disk.MediaType), $($disk.InterfaceType))" }}
if ([uint64]$disk.Size -lt 1MB -or [uint64]$disk.Size -gt 512GB) {{ throw "refusing disk size $($disk.Size)" }}
$scriptPath = Join-Path $env:TEMP ("codex-diskpart-reinit-" + [guid]::NewGuid().ToString("N") + ".txt")
Set-Content -LiteralPath $scriptPath -Encoding ASCII -Value {ps_quote(diskpart_script)}
try {{
  $output = diskpart /s $scriptPath 2>&1
  $exit = $LASTEXITCODE
}} finally {{
  Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
}}
$volumes = Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,VolumeName,FileSystem,DriveType,Size,FreeSpace,VolumeSerialNumber
[pscustomobject]@{{
  disk_index=$diskIndex
  model=$disk.Model
  serial=$disk.SerialNumber
  size=[uint64]$disk.Size
  filesystem={ps_quote(filesystem)}
  label={ps_quote(label)}
  drive_letter={ps_quote(drive_letter)}
  diskpart_exit_code=$exit
  diskpart_output=($output -join "`n")
  logical_disks=$volumes
}} | ConvertTo-Json -Depth 5
if ($exit -ne 0) {{ exit $exit }}
""".strip()
    summary = f"destructively reinitialize removable disk {disk_index} as {filesystem.upper()} {drive_letter}:"
    return RenderedAction("removable_disk_reinitialize", command, clamp_timeout(timeout, 300, 1800), summary)


def removable_volume_format(args: dict[str, Any], timeout: Any = None) -> RenderedAction:
    require_keys(args, {"drive_letter", "filesystem", "label"})
    drive_letter = str(args.get("drive_letter") or "E").strip().upper().rstrip(":")
    if not re.fullmatch(r"[D-Z]", drive_letter):
        raise ValueError("drive_letter must be D..Z")
    filesystem = str(args.get("filesystem") or "exfat").strip().lower()
    if filesystem not in {"exfat", "fat32", "ntfs"}:
        raise ValueError("filesystem must be exfat, fat32, or ntfs")
    label = str(args.get("label") or "SDCARD").strip()
    if not VOLUME_LABEL_RE.fullmatch(label):
        raise ValueError("label contains unsupported characters")
    command = rf"""
$ErrorActionPreference = 'Stop'
$drive = {ps_quote(drive_letter + ":")}
$volume = Get-CimInstance Win32_LogicalDisk | Where-Object {{ $_.DeviceID -eq $drive }}
if (-not $volume) {{ throw "drive $drive was not found" }}
if ($volume.DriveType -ne 2) {{ throw "refusing to format non-removable drive $drive (DriveType=$($volume.DriveType))" }}
$output = cmd.exe /c "format $drive /FS:{filesystem.upper()} /Q /Y /V:{label}" 2>&1
$exit = $LASTEXITCODE
$after = Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,VolumeName,FileSystem,DriveType,Size,FreeSpace,VolumeSerialNumber
[pscustomobject]@{{
  drive=$drive
  filesystem={ps_quote(filesystem)}
  label={ps_quote(label)}
  format_exit_code=$exit
  format_output=($output -join "`n")
  logical_disks=$after
}} | ConvertTo-Json -Depth 5
if ($exit -ne 0) {{ exit $exit }}
""".strip()
    return RenderedAction("removable_volume_format", command, clamp_timeout(timeout, 300, 1800), f"quick format removable volume {drive_letter}: as {filesystem.upper()}")


POLICY = {
    "admin_whoami": {
        "description": "Return elevated identity and admin-token state.",
        "args": {},
        "renderer": fixed_action(
            "admin_whoami",
            "[Security.Principal.WindowsIdentity]::GetCurrent() | "
            "Select-Object Name,AuthenticationType,IsAuthenticated | ConvertTo-Json -Depth 3; "
            "$p=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent(); "
            "[pscustomobject]@{IsAdministrator=$p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)} | ConvertTo-Json",
            "check elevated broker identity",
            20,
            60,
        ),
    },
    "defender_quick_scan": {
        "description": "Start Microsoft Defender quick scan.",
        "args": {},
        "renderer": fixed_action("defender_quick_scan", "Start-MpScan -ScanType QuickScan", "start Defender quick scan", 120, 600),
    },
    "windows_update_scan": {
        "description": "Ask Windows Update client to start a scan.",
        "args": {},
        "renderer": fixed_action("windows_update_scan", "UsoClient StartScan", "start Windows Update scan", 60, 120),
    },
    "flush_dns": {
        "description": "Flush local DNS resolver cache.",
        "args": {},
        "renderer": fixed_action("flush_dns", "Clear-DnsClientCache", "flush DNS client cache", 20, 60),
    },
    "removable_disk_image": {
        "description": "Create a read-only raw image of a USB/removable disk under X:\\CodexRecovery.",
        "args": {"disk_index": "Win32_DiskDrive index", "output_root": "absolute X:\\CodexRecovery path", "chunk_mb": "1..32"},
        "renderer": removable_disk_image,
    },
    "removable_disk_probe": {
        "description": "Read a few sample sectors from a USB/removable disk without writing to it.",
        "args": {"disk_index": "Win32_DiskDrive index"},
        "renderer": removable_disk_probe,
    },
    "removable_disk_reinitialize": {
        "description": "Destructively clean, repartition, quick-format, and assign a USB/removable disk.",
        "args": {"disk_index": "Win32_DiskDrive index", "filesystem": "exfat|fat32|ntfs", "label": "volume label", "drive_letter": "D..Z"},
        "renderer": removable_disk_reinitialize,
    },
    "removable_volume_format": {
        "description": "Destructively quick-format an existing removable drive letter.",
        "args": {"drive_letter": "D..Z", "filesystem": "exfat|fat32|ntfs", "label": "volume label"},
        "renderer": removable_volume_format,
    },
    "service_control": {
        "description": "Start, stop, or restart a named Windows service.",
        "args": {"name": "service name", "action": "start|stop|restart"},
        "renderer": service_control,
    },
    "persistent_route_add": {
        "description": "Add a persistent IPv4 route with validated fields.",
        "args": {"destination": "IPv4", "mask": "IPv4 mask", "gateway": "IPv4", "interface": "ifIndex", "metric": "1..9999"},
        "renderer": persistent_route_add,
    },
    "persistent_route_delete": {
        "description": "Delete a persistent IPv4 route by destination.",
        "args": {"destination": "IPv4"},
        "renderer": persistent_route_delete,
    },
    "enable_wake_device": {
        "description": "Enable Windows wake permission for an existing wake-programmable device.",
        "args": {"device": "exact powercfg wake-programmable device name"},
        "renderer": lambda args, timeout=None: wake_device(args, timeout, enable=True),
    },
    "disable_wake_device": {
        "description": "Disable Windows wake permission for an existing wake-programmable device.",
        "args": {"device": "exact powercfg wake-programmable device name"},
        "renderer": lambda args, timeout=None: wake_device(args, timeout, enable=False),
    },
}


def capabilities() -> dict[str, Any]:
    return {
        "task_name": TASK_NAME,
        "admin_root": str(ADMIN_ROOT),
        "request_ttl_seconds": REQUEST_TTL_SECONDS,
        "actions": {
            name: {"description": spec["description"], "args": spec["args"]}
            for name, spec in sorted(POLICY.items())
        },
    }


def render_action(action: str, args: dict[str, Any] | None = None, timeout: Any = None) -> RenderedAction:
    if action not in POLICY:
        raise ValueError(f"unsupported admin action: {action}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("args must be a JSON object")
    return POLICY[action]["renderer"](args, timeout)


def ensure_dirs() -> None:
    for path in (INBOX, OUTBOX, PROCESSED, LOGS):
        path.mkdir(parents=True, exist_ok=True)


def request_path(request_id: str) -> Path:
    return INBOX / f"{request_id}.json"


def result_path(request_id: str) -> Path:
    return OUTBOX / f"{request_id}.json"


def audit(event: dict[str, Any]) -> None:
    ensure_dirs()
    payload = {"ts": time.time(), **event}
    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
