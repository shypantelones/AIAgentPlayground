$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = ([BitConverter]::ToString($bytes) -replace '-', '').ToLower()
    (Get-Content .env) -replace 'change-me', $token | Set-Content .env -Encoding ascii
    Write-Host "Created .env with a fresh gateway token. Edit it to add a throwaway API key, then re-run." -ForegroundColor Yellow
    exit 0
}

docker compose pull
docker compose up -d
Write-Host "`nDashboard: http://127.0.0.1:18789/  (token is OPENCLAW_GATEWAY_TOKEN in .env)"
Write-Host "Onboarding (first run): .\scripts\cli.ps1 onboard"
