from __future__ import annotations

import base64
import json
import os
import posixpath
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paramiko
from dotenv import load_dotenv
from mcp_runtime import configure_stdio, create_mcp_server
from ssh_auth import ssh_connect_kwargs


ROOT = Path(__file__).resolve().parent
configure_stdio()
load_dotenv(ROOT / ".env", encoding="utf-8-sig")

mcp = create_mcp_server("comfy-ai")


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


def _exec(command: str, timeout: int = 60, sudo: bool = False) -> dict[str, Any]:
    cfg = _config()
    started = time.time()
    command = f"export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8 PYTHONUTF8=1; {command}"
    if sudo:
        if not cfg.sudo_password:
            raise RuntimeError("REMOTE_SUDO_PASSWORD or REMOTE_PASSWORD must be set for sudo commands")
        remote_command = f"sudo -S -p '' bash -lc {shlex.quote(command)}"
    else:
        remote_command = f"bash -lc {shlex.quote(command)}"

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
        "sudo": sudo,
        "exit_code": exit_code,
        "stdout": out,
        "stderr": err,
        "duration_seconds": round(time.time() - started, 3),
    }


def _json_remote(script: str, timeout: int = 60, sudo: bool = False) -> dict[str, Any]:
    result = _exec(script, timeout=timeout, sudo=sudo)
    try:
        result["data"] = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


def _b64_json(data: Any) -> str:
    return base64.b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode("ascii")


def _safe_output_name(prefix: str = "mcp_sdxl") -> str:
    safe = "".join(c for c in prefix if c.isalnum() or c in {"-", "_"}).strip("_-")
    if not safe:
        safe = "mcp_sdxl"
    return f"{safe}_{int(time.time())}.png"


@mcp.tool()
def ai_service_status() -> dict[str, Any]:
    """Return status for ComfyUI, the fast Vulkan UI, Apache, ports, and AI disk usage."""
    script = r"""
python3 - <<'PY'
import json, os, shutil, subprocess
def run(cmd):
    return subprocess.run(cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
services={}
for name in ["comfyui.service", "sdcpp-ui.service", "apache2.service"]:
    r=run(f"systemctl is-active {name}")
    services[name]=r.stdout.strip()
ports=run("ss -ltnp | grep -E ':(80|8188|8190)' || true").stdout
du=run("du -sh /mnt/ai/models/checkpoints /mnt/ai/outputs /mnt/ai/outputs/sdcpp-ui 2>/dev/null || true").stdout
disk=shutil.disk_usage("/mnt")
print(json.dumps({
    "services": services,
    "ports": ports,
    "disk_mnt": {"total": disk.total, "used": disk.used, "free": disk.free},
    "du": du,
}, ensure_ascii=False))
PY
"""
    return _json_remote(script, timeout=30, sudo=True)


@mcp.tool()
def list_checkpoints() -> dict[str, Any]:
    """List installed checkpoint models visible to ComfyUI and the fast SDXL runner."""
    script = r"""
python3 - <<'PY'
import json
from pathlib import Path
base=Path('/mnt/ai/models/checkpoints')
rows=[]
for p in sorted(base.glob('*')):
    if p.suffix.lower() in {'.safetensors', '.ckpt'}:
        rows.append({"name": p.name, "path": str(p), "size": p.stat().st_size})
print(json.dumps(rows, ensure_ascii=False))
PY
"""
    return _json_remote(script, timeout=30, sudo=True)


@mcp.tool()
def list_workflow_assets() -> dict[str, Any]:
    """List saved workflow assets under /mnt/ai/workflows."""
    script = r"""
python3 - <<'PY'
import json
from pathlib import Path
base=Path('/mnt/ai/workflows')
rows=[{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in sorted(base.glob('*')) if p.is_file()]
print(json.dumps(rows, ensure_ascii=False))
PY
"""
    return _json_remote(script, timeout=30, sudo=True)


@mcp.tool()
def comfy_system_stats() -> dict[str, Any]:
    """Read ComfyUI /system_stats from the server-local API."""
    script = r"""
python3 - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8188/system_stats', timeout=15) as r:
    print(json.dumps(json.load(r), ensure_ascii=False))
PY
"""
    return _json_remote(script, timeout=30, sudo=True)


@mcp.tool()
def comfy_queue() -> dict[str, Any]:
    """Read ComfyUI queue state."""
    script = r"""
python3 - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8188/queue', timeout=15) as r:
    print(json.dumps(json.load(r), ensure_ascii=False))
PY
"""
    return _json_remote(script, timeout=30, sudo=True)


@mcp.tool()
def comfy_interrupt() -> dict[str, Any]:
    """Interrupt the current ComfyUI generation."""
    script = r"""
python3 - <<'PY'
import json, urllib.request
req=urllib.request.Request('http://127.0.0.1:8188/interrupt', data=b'{}', headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=15) as r:
    body=r.read().decode('utf-8', errors='replace')
print(json.dumps({"status": "sent", "response": body}, ensure_ascii=False))
PY
"""
    return _json_remote(script, timeout=30, sudo=True)


@mcp.tool()
def comfy_restart() -> dict[str, Any]:
    """Restart ComfyUI and wait until its local HTTP API responds."""
    script = r"""
systemctl restart comfyui.service
python3 - <<'PY'
import json, time, urllib.request
ok=False
err=None
for _ in range(60):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8188/system_stats', timeout=2) as r:
            ok = r.status == 200
        if ok:
            break
    except Exception as exc:
        err=str(exc)
    time.sleep(2)
print(json.dumps({"ready": ok, "last_error": err}, ensure_ascii=False))
PY
"""
    return _json_remote(script, timeout=150, sudo=True)


@mcp.tool()
def comfy_generate_from_asset(
    workflow_path: str = "/mnt/ai/workflows/comfy_sdxl_adult_boudoir_starter_api.json",
    prompt: str | None = None,
    negative_prompt: str | None = None,
    checkpoint: str | None = None,
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    wait: bool = True,
    timeout: int = 1200,
) -> dict[str, Any]:
    """Queue an API-format ComfyUI workflow asset, optionally overriding common SDXL fields."""
    if not workflow_path.startswith("/mnt/ai/workflows/"):
        raise ValueError("workflow_path must be under /mnt/ai/workflows")
    overrides: dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "checkpoint": checkpoint,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg": cfg,
        "wait": wait,
        "timeout": timeout,
    }
    encoded = _b64_json(overrides)
    script = f"""
python3 - <<'PY'
import base64, json, time, urllib.parse, urllib.request
workflow_path={workflow_path!r}
overrides=json.loads(base64.b64decode({encoded!r}).decode('utf-8'))
with open(workflow_path, encoding='utf-8') as f:
    prompt=json.load(f)
if overrides.get('checkpoint') and '4' in prompt:
    prompt['4']['inputs']['ckpt_name']=overrides['checkpoint']
if overrides.get('width') is not None and '5' in prompt:
    prompt['5']['inputs']['width']=int(overrides['width'])
if overrides.get('height') is not None and '5' in prompt:
    prompt['5']['inputs']['height']=int(overrides['height'])
if overrides.get('prompt') and '6' in prompt:
    prompt['6']['inputs']['text']=overrides['prompt']
if overrides.get('negative_prompt') and '7' in prompt:
    prompt['7']['inputs']['text']=overrides['negative_prompt']
if overrides.get('steps') is not None and '8' in prompt:
    prompt['8']['inputs']['steps']=int(overrides['steps'])
if overrides.get('cfg') is not None and '8' in prompt:
    prompt['8']['inputs']['cfg']=float(overrides['cfg'])
req=urllib.request.Request('http://127.0.0.1:8188/prompt', data=json.dumps({{'prompt':prompt}}).encode(), headers={{'Content-Type':'application/json'}})
started=time.time()
with urllib.request.urlopen(req, timeout=30) as r:
    queued=json.load(r)
pid=queued['prompt_id']
result={{'prompt_id': pid, 'queued': queued}}
if overrides.get('wait'):
    timeout=float(overrides.get('timeout') or 1200)
    while time.time()-started < timeout:
        with urllib.request.urlopen('http://127.0.0.1:8188/history/'+urllib.parse.quote(pid), timeout=30) as r:
            hist=json.load(r)
        if pid in hist:
            result['history']=hist[pid]
            result['seconds']=round(time.time()-started, 2)
            break
        time.sleep(5)
    else:
        result['timeout']=True
print(json.dumps(result, ensure_ascii=False))
PY
"""
    return _json_remote(script, timeout=timeout + 60, sudo=True)


def generate_fast_sdxl(
    prompt: str,
    negative_prompt: str = "low quality, blurry, watermark, text, bad anatomy, deformed",
    checkpoint: str = "DreamShaperXL_Turbo_v2_1.safetensors",
    width: int = 768,
    height: int = 768,
    steps: int = 6,
    cfg: float = 2.0,
    sampler: str = "euler",
    output_prefix: str = "mcp_sdxl",
    timeout: int = 1800,
) -> dict[str, Any]:
    """Generate one image through stable-diffusion.cpp Vulkan on the RX 580 and return path/URL."""
    allowed_samplers = {"euler", "euler_a", "dpm++2m", "dpm++2mv2", "lcm"}
    if sampler not in allowed_samplers:
        raise ValueError(f"sampler must be one of {sorted(allowed_samplers)}")
    width = max(256, min(1536, int(width)))
    height = max(256, min(1536, int(height)))
    steps = max(1, min(50, int(steps)))
    cfg = max(0.5, min(20.0, float(cfg)))
    out_name = _safe_output_name(output_prefix)
    model_path = posixpath.join("/mnt/ai/models/checkpoints", posixpath.basename(checkpoint))
    out_path = posixpath.join("/mnt/ai/outputs/sdcpp-ui", out_name)
    command = " ".join(
        [
            "cd /mnt/ai/sd-cpp/stable-diffusion.cpp &&",
            "/usr/bin/time -v ./build-vulkan/bin/sd-cli",
            "-m",
            shlex.quote(model_path),
            "-p",
            shlex.quote(prompt),
            "-n",
            shlex.quote(negative_prompt),
            "-o",
            shlex.quote(out_path),
            "-W",
            str(width),
            "-H",
            str(height),
            "--steps",
            str(steps),
            "--cfg-scale",
            str(cfg),
            "--sampling-method",
            shlex.quote(sampler),
            "--rng cpu",
            "--backend diffusion=vulkan0,clip=cpu,vae=cpu",
            "--params-backend diffusion=vulkan0,clip=cpu,vae=cpu",
            "--clip-on-cpu --vae-on-cpu --max-vram 7 --stream-layers",
        ]
    )
    result = _exec(command, timeout=timeout, sudo=False)
    result["output_path"] = out_path
    public_url = os.getenv("COMFY_PUBLIC_URL", "http://comfy.example.lan").rstrip("/")
    result["output_url"] = f"{public_url}/outputs/{out_name}"
    return result


if __name__ == "__main__":
    mcp.run()
