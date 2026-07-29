$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

& $python - <<'PY'
import json
from steamdeck_mcp import deck_probe

result = deck_probe()
print(json.dumps(result, ensure_ascii=False, indent=2))
if result.get("returncode") not in (0, None):
    raise SystemExit(result.get("returncode"))
PY
