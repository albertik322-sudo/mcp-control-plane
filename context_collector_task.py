from __future__ import annotations

import json
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import codex_memory as mem
import context_collector


ROOT = Path(__file__).resolve().parent
LOG = ROOT / ".runtime" / "context_collector.log"


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8", errors="replace") as log, redirect_stdout(log), redirect_stderr(log):
        print(f"\n[{mem.iso()}] scheduled context collection started", flush=True)
        try:
            result = context_collector.collect_selected()
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            print(f"[{mem.iso()}] scheduled context collection finished", flush=True)
            return 0
        except Exception:
            traceback.print_exc()
            print(f"[{mem.iso()}] scheduled context collection failed", flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
