# Kick off a demo workflow from the CLI and live-stream progress.
# Pass -ResetMemory for the "first run" demo (so the lesson recording fires).
# Pass -WorkflowId <id> to attach to an existing workflow.
param(
    [switch]$ResetMemory,
    [string]$WorkflowId = "",
    [string]$Question = ""
)

$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$cliArgs = @()
if ($ResetMemory) { $cliArgs += "--reset-memory" }
if ($WorkflowId)  { $cliArgs += "--workflow-id"; $cliArgs += $WorkflowId }
if ($Question)    { $cliArgs += "--question"; $cliArgs += $Question }

& $py (Join-Path $root "run_demo.py") @cliArgs
