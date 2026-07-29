from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import server


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
AUDIT_LOG = ROOT / "ui_audit.jsonl"


def _json_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _audit(event: str, payload: dict[str, Any], result: dict[str, Any] | None = None) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "payload": {k: v for k, v in payload.items() if "password" not in k.lower()},
        "exit_code": None if result is None else result.get("exit_code"),
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _collect_metrics() -> dict[str, Any]:
    script = r"""
set -u
printf 'host=%s\n' "$(hostname)"
printf 'now=%s\n' "$(date -Is)"
printf 'uptime=%s\n' "$(uptime -p 2>/dev/null || true)"
printf 'loadavg=%s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"
printf 'mem_total_mb=%s\n' "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
printf 'mem_available_mb=%s\n' "$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
printf 'disk_root=%s\n' "$(df -P / | awk 'NR==2 {print $2" "$3" "$4" "$5}')"
if [ -f /var/www/nextcloud/version.php ]; then
  printf 'nextcloud_version=%s\n' "$(php -r 'include "/var/www/nextcloud/version.php"; echo $OC_VersionString ?? "";' 2>/dev/null || true)"
else
  printf 'nextcloud_version=\n'
fi
for svc in nginx apache2 mariadb mysql postgresql redis-server redis docker; do
  systemctl list-unit-files "$svc.service" >/dev/null 2>&1 || continue
  state=$(systemctl is-active "$svc" 2>/dev/null || true)
  printf 'service=%s:%s\n' "$svc" "$state"
done
systemctl list-unit-files 'php*-fpm.service' --no-legend 2>/dev/null | awk '{print $1}' | while read -r unit; do
  [ -n "$unit" ] || continue
  svc=${unit%.service}
  state=$(systemctl is-active "$svc" 2>/dev/null || true)
  printf 'service=%s:%s\n' "$svc" "$state"
done
"""
    result = server.run_sudo_command(script, timeout=45)
    if result["exit_code"] != 0:
        return {"ok": False, "raw": result}

    values: dict[str, Any] = {"services": []}
    for line in result["stdout"].splitlines():
        key, _, value = line.partition("=")
        if key == "service":
            name, _, state = value.partition(":")
            values["services"].append({"name": name, "state": state or "unknown"})
        elif key:
            values[key] = value

    mem_total = int(values.get("mem_total_mb") or 0)
    mem_available = int(values.get("mem_available_mb") or 0)
    disk_parts = str(values.get("disk_root", "")).split()
    data = {
        "host": values.get("host", ""),
        "now": values.get("now", ""),
        "uptime": values.get("uptime", ""),
        "loadavg": values.get("loadavg", ""),
        "memory": {
            "total_mb": mem_total,
            "used_mb": max(mem_total - mem_available, 0),
            "available_mb": mem_available,
        },
        "disk_root": {},
        "nextcloud_version": values.get("nextcloud_version", ""),
        "services": values["services"],
    }
    if len(disk_parts) == 4:
        data["disk_root"] = {
            "total_kb": int(disk_parts[0]),
            "used_kb": int(disk_parts[1]),
            "available_kb": int(disk_parts[2]),
            "used_percent": int(disk_parts[3].rstrip("%")),
        }
    data["ok"] = True
    return data


async def index(_: Request) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "ui": "running"})


async def metrics(_: Request) -> JSONResponse:
    return JSONResponse(_collect_metrics())


async def command(request: Request) -> JSONResponse:
    body = await request.json()
    command_text = str(body.get("command", "")).strip()
    cwd = str(body.get("cwd", "")).strip() or None
    sudo = bool(body.get("sudo", False))
    timeout = int(body.get("timeout", 60))
    if not command_text:
        return _json_error("command is required")
    if timeout < 1 or timeout > 600:
        return _json_error("timeout must be between 1 and 600 seconds")

    payload = {"command": command_text, "cwd": cwd, "sudo": sudo, "timeout": timeout}
    result = server.run_sudo_command(command_text, cwd=cwd, timeout=timeout) if sudo else server.run_command(command_text, cwd=cwd, timeout=timeout)
    _audit("command", payload, result)
    return JSONResponse({"ok": result["exit_code"] == 0, "result": result})


async def occ(request: Request) -> JSONResponse:
    body = await request.json()
    args = str(body.get("args", "")).strip()
    timeout = int(body.get("timeout", 120))
    if not args:
        return _json_error("args are required")
    result = server.nextcloud_occ(args, timeout=timeout)
    _audit("occ", {"args": args, "timeout": timeout}, result)
    return JSONResponse({"ok": result["exit_code"] == 0, "result": result})


routes = [
    Route("/", index),
    Route("/api/health", health),
    Route("/api/metrics", metrics),
    Route("/api/command", command, methods=["POST"]),
    Route("/api/occ", occ, methods=["POST"]),
]

app = Starlette(debug=False, routes=routes)


INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ubuntu Control</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1115;
      --panel: #181b22;
      --panel-2: #20242d;
      --text: #eceff4;
      --muted: #9aa4b2;
      --line: #303642;
      --accent: #47c2a8;
      --warn: #f2b84b;
      --bad: #ef6b73;
      --good: #72d17c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 "Segoe UI", system-ui, sans-serif;
    }
    header {
      height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 24px;
      border-bottom: 1px solid var(--line);
      background: #12151b;
    }
    h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: 0; }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      min-height: calc(100vh - 64px);
    }
    aside, section { padding: 20px; }
    aside { border-right: 1px solid var(--line); background: #12151b; }
    .grid { display: grid; gap: 12px; }
    .metrics { grid-template-columns: 1fr 1fr; }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .label { color: var(--muted); font-size: 12px; }
    .value { margin-top: 6px; font-size: 20px; font-weight: 700; overflow-wrap: anywhere; }
    .bar {
      height: 8px;
      background: #0c0e12;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 10px;
    }
    .bar > i { display: block; height: 100%; background: var(--accent); width: 0; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    button, input, textarea {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      font: inherit;
    }
    button {
      min-height: 36px;
      padding: 0 12px;
      cursor: pointer;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: #07110f; font-weight: 700; }
    button:disabled { opacity: .55; cursor: wait; }
    input { height: 36px; padding: 0 10px; }
    textarea {
      width: 100%;
      min-height: 132px;
      resize: vertical;
      padding: 12px;
      font-family: "Cascadia Mono", Consolas, monospace;
    }
    pre {
      margin: 0;
      min-height: 330px;
      max-height: 54vh;
      overflow: auto;
      padding: 14px;
      border-radius: 8px;
      background: #090b0f;
      border: 1px solid var(--line);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: "Cascadia Mono", Consolas, monospace;
    }
    .quick { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
    .pill {
      display: inline-flex;
      align-items: center;
      height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      background: var(--panel-2);
      color: var(--muted);
      border: 1px solid var(--line);
      font-size: 12px;
    }
    .pill.good { color: var(--good); }
    .pill.bad { color: var(--bad); }
    .toggle { display: inline-flex; gap: 8px; align-items: center; color: var(--muted); }
    .toggle input { width: 18px; height: 18px; }
    .stack { display: grid; gap: 16px; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Ubuntu Control</h1>
    <div class="row">
      <span id="status" class="pill">checking</span>
      <button id="refresh">Refresh</button>
    </div>
  </header>
  <main>
    <aside class="stack">
      <div class="grid metrics">
        <div class="card"><div class="label">Host</div><div id="host" class="value">-</div></div>
        <div class="card"><div class="label">Uptime</div><div id="uptime" class="value">-</div></div>
        <div class="card"><div class="label">Load</div><div id="load" class="value">-</div></div>
        <div class="card"><div class="label">Nextcloud</div><div id="nc" class="value">-</div></div>
      </div>
      <div class="card">
        <div class="label">Memory</div>
        <div id="memText" class="value">-</div>
        <div class="bar"><i id="memBar"></i></div>
      </div>
      <div class="card">
        <div class="label">Disk /</div>
        <div id="diskText" class="value">-</div>
        <div class="bar"><i id="diskBar"></i></div>
      </div>
      <div class="card">
        <div class="label">Services</div>
        <div id="services" class="row" style="margin-top:10px"></div>
      </div>
    </aside>
    <section class="stack">
      <div class="card stack">
        <div class="quick">
          <button data-cmd="systemctl --failed --no-pager">Failed units</button>
          <button data-cmd="df -h && echo && free -h">Storage and memory</button>
          <button data-cmd="journalctl -p 3 -n 80 --no-pager" data-sudo="true">Recent errors</button>
          <button data-occ="status">Nextcloud status</button>
          <button data-occ="app:list">Nextcloud apps</button>
          <button data-occ="maintenance:mode --status">Maintenance mode</button>
        </div>
        <textarea id="command" spellcheck="false" placeholder="bash command"></textarea>
        <div class="row">
          <button id="run" class="primary">Run</button>
          <label class="toggle"><input id="sudo" type="checkbox"> sudo</label>
          <input id="cwd" placeholder="cwd, optional" value="">
          <input id="timeout" type="number" min="1" max="600" value="60" style="width:100px">
        </div>
      </div>
      <pre id="output">Ready.</pre>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const status = $("status");
    const output = $("output");

    function mb(n) { return `${Math.round(n).toLocaleString("ru-RU")} MB`; }
    function pct(part, total) { return total ? Math.round((part / total) * 100) : 0; }
    function setBusy(busy) {
      document.querySelectorAll("button").forEach((b) => b.disabled = busy);
    }
    function renderResult(data) {
      const r = data.result || data.raw || data;
      output.textContent = [
        `exit_code: ${r.exit_code ?? "n/a"}`,
        `duration: ${r.duration_seconds ?? "n/a"}s`,
        "",
        r.stdout || "",
        r.stderr ? `\n[stderr]\n${r.stderr}` : "",
      ].join("\n");
    }
    async function refreshMetrics() {
      status.textContent = "loading";
      status.className = "pill";
      try {
        const res = await fetch("/api/metrics");
        const data = await res.json();
        if (!data.ok) throw new Error(data.raw?.stderr || "metrics failed");
        $("host").textContent = data.host;
        $("uptime").textContent = data.uptime || "-";
        $("load").textContent = data.loadavg;
        $("nc").textContent = data.nextcloud_version || "-";
        const memPercent = pct(data.memory.used_mb, data.memory.total_mb);
        $("memText").textContent = `${mb(data.memory.used_mb)} / ${mb(data.memory.total_mb)} (${memPercent}%)`;
        $("memBar").style.width = `${memPercent}%`;
        const disk = data.disk_root;
        $("diskText").textContent = `${disk.used_percent}% used`;
        $("diskBar").style.width = `${disk.used_percent}%`;
        $("services").innerHTML = "";
        data.services.forEach((svc) => {
          const pill = document.createElement("span");
          pill.className = `pill ${svc.state === "active" ? "good" : "bad"}`;
          pill.textContent = `${svc.name}: ${svc.state}`;
          $("services").appendChild(pill);
        });
        status.textContent = "online";
        status.className = "pill good";
      } catch (err) {
        status.textContent = "offline";
        status.className = "pill bad";
        output.textContent = String(err);
      }
    }
    async function post(url, body) {
      setBusy(true);
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify(body),
        });
        renderResult(await res.json());
        refreshMetrics();
      } finally {
        setBusy(false);
      }
    }
    $("run").addEventListener("click", () => post("/api/command", {
      command: $("command").value,
      sudo: $("sudo").checked,
      cwd: $("cwd").value,
      timeout: Number($("timeout").value || 60),
    }));
    $("refresh").addEventListener("click", refreshMetrics);
    document.querySelectorAll("[data-cmd]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("command").value = btn.dataset.cmd;
        $("sudo").checked = btn.dataset.sudo === "true";
        $("run").click();
      });
    });
    document.querySelectorAll("[data-occ]").forEach((btn) => {
      btn.addEventListener("click", () => post("/api/occ", {args: btn.dataset.occ, timeout: 120}));
    });
    refreshMetrics();
    setInterval(refreshMetrics, 30000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "ui_app:app",
        host=os.getenv("UI_HOST", "127.0.0.1"),
        port=int(os.getenv("UI_PORT", "8765")),
        reload=False,
    )
