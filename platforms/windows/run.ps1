# Windows launcher for the OpenClaw control panel.
#   .\run.ps1                 start the panel and open http://127.0.0.1:8765
#   .\run.ps1 -NoBrowser      start without opening a browser
# If PowerShell refuses to run scripts:  powershell -ExecutionPolicy Bypass -File .\run.ps1
param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$app = Join-Path $here '..\..\control-panel'

# A freshly installed Docker/Python may not be on this session's PATH yet.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

# Load config.env (KEY=VALUE per line). Variables you already set in your environment win.
Get-Content (Join-Path $here 'config.env') | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$' -and $_ -notmatch '^\s*#') {
        if (-not (Test-Path "env:$($Matches[1])")) { Set-Item "env:$($Matches[1])" $Matches[2] }
    }
}

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { throw "Python 3 not found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')." }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Warning "docker not found on PATH. Install and start Docker Desktop." }

$port = if ($env:PANEL_PORT) { $env:PANEL_PORT } else { '8765' }
if (-not $NoBrowser -and -not $env:OPENCLAW_NO_BROWSER) { Start-Process "http://127.0.0.1:$port" }
Set-Location $app
& $py.Source app.py
