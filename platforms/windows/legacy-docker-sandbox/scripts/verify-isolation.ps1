# Proves the sandbox is actually isolated. Run after up.ps1. Every check should print PASS.
Set-Location (Split-Path $PSScriptRoot)

# Runs a shell command in the gateway container; returns its exit code (0 = command succeeded)
function Rc([string]$sh) {
    docker compose exec -T gateway sh -c $sh *> $null
    return $LASTEXITCODE
}
function Show($name, [bool]$ok) {
    $color = if ($ok) { 'Green' } else { 'Red' }
    $label = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("{0,-52} {1}" -f $name, $label) -ForegroundColor $color
}

$fetch = "fetch('https://example.com',{signal:AbortSignal.timeout(6000)}).then(()=>process.exit(0)).catch(()=>process.exit(1))"

Show "Direct internet blocked (proxy bypassed)"  ((Rc "env -u HTTP_PROXY -u HTTPS_PROXY -u NODE_USE_ENV_PROXY node -e `"$fetch`"") -ne 0)
Show "Non-allowlisted domain blocked via proxy"   ((Rc "node -e `"$fetch`"") -ne 0)
Show "Root filesystem is read-only"               ((Rc "touch /usr/x") -ne 0)
Show "No Linux capabilities (CapEff = 0)"         ((Rc "grep -q '^CapEff:.0000000000000000' /proc/self/status") -eq 0)
Show "No docker socket inside container"          ((Rc "test -e /var/run/docker.sock") -ne 0)
Show "Host C: drive not visible"                  ((Rc "test -e /mnt/c || test -e /c || test -e /host_mnt") -ne 0)
Show "Not running as root"                        ((docker compose exec -T gateway id -u) -ne "0")
$port = docker compose port ui-forward 18789
Show "Dashboard bound to loopback only"           ($port -match '^127\.0\.0\.1:')
