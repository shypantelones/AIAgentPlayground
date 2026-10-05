#!/bin/sh
# Root helper: turns IP forwarding on or off inside ONE container lab node. Only the control panel may run it, through
# sudo, and only this path (see control-panel/README.md, "Container labs"). It does nothing else.
#   forward.sh <container name> <1|0>
# The name must be a lab node container (aglab-<8 hex>-<node>) that carries the lab label and is running. The sysctl
# is written in that container's network namespace only: docker and the host's own forwarding are not touched.
# The agent never gets this path. Install it root-owned, outside the repo, so the panel user can't edit it.
set -eu
name=${1:-}
val=${2:-}
# the same shape container_lab.NODE_CONTAINER_RE accepts: aglab-<8 hex>-<node name>
if ! printf '%s\n' "$name" | grep -Eq '^aglab-[0-9a-f]{8}-[a-z][a-z0-9]{0,14}$'; then
    echo "not a lab node: $name" >&2
    exit 2
fi
case "$val" in
    0|1) ;;
    *) echo "value must be 0 or 1" >&2; exit 2 ;;
esac
info=$(docker inspect -f '{{index .Config.Labels "aiagentplayground-lab"}} {{.State.Running}} {{.State.Pid}}' "$name" 2>/dev/null) \
    || { echo "no such container: $name" >&2; exit 2; }
set -- $info                       # label, running, pid
if [ -z "$1" ] || [ "$1" = "<no" ] || [ "$2" != "true" ]; then
    echo "not a running lab container: $name" >&2
    exit 2
fi
exec nsenter -t "$3" -n sysctl -w net.ipv4.ip_forward="$val"
