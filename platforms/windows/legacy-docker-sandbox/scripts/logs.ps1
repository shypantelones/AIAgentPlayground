# Watch what the agent tries to reach. TCP_DENIED lines = blocked egress attempts.
Set-Location (Split-Path $PSScriptRoot)
docker compose exec egress-proxy tail -f /var/log/squid/access.log
