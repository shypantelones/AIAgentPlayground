# Run an OpenClaw CLI command inside the sandboxed gateway container, e.g.  .\scripts\cli.ps1 onboard
Set-Location (Split-Path $PSScriptRoot)
docker compose exec -it gateway node dist/index.js @args
