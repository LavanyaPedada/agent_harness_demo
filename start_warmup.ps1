# Pre-load the demo model into Ollama memory so the first chat call doesn't
# pay the model-load tax (~10-15s on first invocation).
#
# Run once before the demo, after start_temporal.ps1 + start_worker.ps1.
$model = if ($env:DEMO_MODEL) { $env:DEMO_MODEL } else { "qwen2.5:3b" }
Write-Host "[warmup] pinging $model on Ollama ..."

$body = @{
    model = $model
    prompt = "ping"
    stream = $false
    options = @{ num_predict = 1 }
} | ConvertTo-Json -Compress

try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-RestMethod -Uri "http://localhost:11434/api/generate" -Method POST -ContentType "application/json" -Body $body -TimeoutSec 120
    $sw.Stop()
    Write-Host ("[warmup] {0} loaded in {1:N1}s ({2} eval tokens)" -f $model, ($sw.Elapsed.TotalSeconds), $r.eval_count)
    Write-Host "[warmup] subsequent calls will hit the model already in memory."
} catch {
    Write-Host "[warmup] failed: $($_.Exception.Message)"
    exit 1
}
