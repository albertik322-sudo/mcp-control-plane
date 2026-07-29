from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp_runtime import configure_stdio, powershell_base_args, powershell_executable
from windows_admin_policy import (
    INBOX,
    MAX_OUTPUT_CHARS,
    PROCESSED,
    ROOT,
    audit,
    ensure_dirs,
    render_action,
    result_path,
)


LOCK_DIR = ROOT / ".runtime" / "windows_admin" / "broker.lock"


def compact(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


def acquire_lock() -> bool:
    try:
        LOCK_DIR.mkdir(parents=True)
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    try:
        shutil.rmtree(LOCK_DIR)
    except FileNotFoundError:
        pass


def write_result(request_id: str, payload: dict[str, Any]) -> None:
    ensure_dirs()
    tmp = result_path(request_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(result_path(request_id))


def process_request(path: Path) -> dict[str, Any]:
    started = time.time()
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        request_id = str(request["request_id"])
        created_at = float(request.get("created_at") or started)
        if started - created_at > 300:
            raise TimeoutError("request expired before broker execution")
        rendered = render_action(str(request["action"]), request.get("args") or {}, request.get("timeout"))
        completed = subprocess.run(
            [*powershell_base_args(), "-Command", rendered.command],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=rendered.timeout,
        )
        payload = {
            "ok": completed.returncode == 0,
            "request_id": request_id,
            "action": rendered.action,
            "summary": rendered.summary,
            "runtime": powershell_executable(),
            "command": rendered.command,
            "exit_code": completed.returncode,
            "stdout": compact(completed.stdout),
            "stderr": compact(completed.stderr),
            "duration_seconds": round(time.time() - started, 3),
            "finished_at": time.time(),
        }
    except Exception as exc:
        request_id = path.stem
        payload = {
            "ok": False,
            "request_id": request_id,
            "error": str(exc),
            "type": type(exc).__name__,
            "duration_seconds": round(time.time() - started, 3),
            "finished_at": time.time(),
        }
    write_result(request_id, payload)
    audit(
        {
            "event": "admin_request_processed",
            "request_id": request_id,
            "ok": payload.get("ok"),
            "action": payload.get("action"),
            "exit_code": payload.get("exit_code"),
            "duration_seconds": payload.get("duration_seconds"),
        }
    )
    try:
        path.replace(PROCESSED / path.name)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    return payload


def main() -> int:
    configure_stdio()
    ensure_dirs()
    if not acquire_lock():
        audit({"event": "broker_skipped", "reason": "lock_exists"})
        return 2
    try:
        results = []
        for path in sorted(INBOX.glob("*.json")):
            results.append(process_request(path))
        print(json.dumps({"processed": len(results), "results": results}, ensure_ascii=False, default=str))
        return 0 if all(item.get("ok") for item in results) else 1
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
