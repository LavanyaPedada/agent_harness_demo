# Launches the FastAPI chat UI at http://localhost:8001
# Requires: Temporal dev server running (start_temporal.ps1) and Ollama running.
# The app auto-starts a Temporal worker in-process on startup, so you do NOT
# need start_worker.ps1 as well when using the web UI.
#
# DEMO_FAST + DEMO_MODEL control the LLM behaviour (see start_worker.ps1).
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$env:DEMO_FAST = "1"
if (-not $env:DEMO_MODEL) { $env:DEMO_MODEL = "qwen2.5:3b" }
Write-Host "[app] DEMO_FAST=$($env:DEMO_FAST)  DEMO_MODEL=$($env:DEMO_MODEL)  PORT=8001"
& $py -m uvicorn app.server:app `
    --app-dir $root `
    --host 127.0.0.1 `
    --port 8001 `
    --log-level info
