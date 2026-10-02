param([switch]$Wipe)  # -Wipe also deletes this stack's state and workspace volumes (NOT the shared model volume unless this compose project owns it - see warning below)
Set-Location (Split-Path $PSScriptRoot)
if ($Wipe) {
    Write-Warning "-Wipe removes ALL volumes declared in this compose file, including 'ollama-models' (the downloaded model, ~9 GB, also used by control-panel)."
    docker compose down -v --remove-orphans
    Write-Host "Wiped containers and volumes."
} else {
    docker compose down --remove-orphans
}
