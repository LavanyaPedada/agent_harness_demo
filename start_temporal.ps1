# Start the Temporal dev server (in-memory, single process).
# Web UI: http://localhost:8233   gRPC: localhost:7233
#
# Requires the Temporal CLI. Install it (https://docs.temporal.io/cli) so
# `temporal` is on your PATH, or set $env:TEMPORAL_EXE to temporal.exe's path.
$temporal = if ($env:TEMPORAL_EXE) { $env:TEMPORAL_EXE } else { "temporal" }
& $temporal server start-dev `
    --ui-port 8233 `
    --port 7233 `
    --log-level warn
