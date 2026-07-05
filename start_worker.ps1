# Start the Temporal worker (registers workflow + activities).
# Ctrl+C to kill mid-run; restart to see Temporal resume the workflow.
#
# NOTE: the web app (start_app.ps1) already runs a worker in-process, so you
# only need this separate worker if you drive the demo via run.ps1 / run_demo.py.
#
# DEMO_FAST=1 makes the planner skip its LLM call (uses a deterministic plan)
# so first-run wall time drops to ~30-50s. The coding-agent self-correction
# and the final executive insight remain real LLM calls.
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$env:DEMO_FAST = "1"
if (-not $env:DEMO_MODEL) { $env:DEMO_MODEL = "qwen2.5:3b" }
Write-Host "[worker] DEMO_FAST=$($env:DEMO_FAST)  DEMO_MODEL=$($env:DEMO_MODEL)"
& $py (Join-Path $root "worker.py")
