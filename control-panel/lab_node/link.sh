#!/bin/sh
# Root helper: creates ONE point-to-point lab link between two node containers of one lab. Only the control panel
# may run it, through sudo, and only this path (see control-panel/README.md, "Container labs"). It does nothing else.
#   link.sh <lab id> <link index> <container a> <iface a> <container b> <iface b>
# Both containers must be running and carry the lab's label. Each end becomes an interface in its own node's network
# namespace (eth1..eth99). The pair is a veth: no bridge, so frames can't leak onto another link. A failure part-way
# removes whatever was made, so no half link is left behind.
set -eu
rid=${1:-}; idx=${2:-}; ca=${3:-}; ia=${4:-}; cb=${5:-}; ib=${6:-}

fail() { echo "$1" >&2; exit 2; }
printf '%s\n' "$rid" | grep -Eq '^[0-9a-f]{8}$' || fail "bad lab id"
printf '%s\n' "$idx" | grep -Eq '^[0-9]{1,2}$' || fail "bad link index"
for c in "$ca" "$cb"; do
    printf '%s\n' "$c" | grep -Eq "^aglab-$rid-[a-z][a-z0-9]{0,14}$" || fail "not a node of lab $rid: $c"
done
[ "$ca" != "$cb" ] || fail "a link needs two different nodes"
for i in "$ia" "$ib"; do
    printf '%s\n' "$i" | grep -Eq '^eth[1-9][0-9]?$' || fail "bad interface name: $i"
done

# check that a container is a running node of this lab; sets PID to its pid. A function that runs in the shell itself
# (not in $(...)), so a failure here really stops the script.
check_node() {
    name=$1
    info=$(docker inspect -f '{{.State.Pid}} {{.State.Running}} {{index .Config.Labels "aiagentplayground-lab"}}' "$name" 2>/dev/null) \
        || fail "no such container: $name"
    set -- $info
    [ "$2" = true ] || fail "not running: $name"
    [ "$3" = "$rid" ] || fail "not a node of lab $rid: $name"
    PID=$1
}
check_node "$ca"; pa=$PID
check_node "$cb"; pb=$PID
nsenter -t "$pa" -n ip link show "$ia" >/dev/null 2>&1 && fail "$ca already has $ia"
nsenter -t "$pb" -n ip link show "$ib" >/dev/null 2>&1 && fail "$cb already has $ib"

# host-side names: 14 characters at most (the kernel's limit is 15)
va="agv${rid}a${idx}"
vb="agv${rid}b${idx}"
ip link show "$va" >/dev/null 2>&1 && fail "stale link $va is still on the host; remove it first"

undo() {                                       # remove both ends wherever they are now
    nsenter -t "$pa" -n ip link del "$ia" 2>/dev/null || true
    nsenter -t "$pb" -n ip link del "$ib" 2>/dev/null || true
    ip link del "$va" 2>/dev/null || true
    ip link del "$vb" 2>/dev/null || true
}
ip link add "$va" type veth peer name "$vb" || fail "could not create the link"
if ! { ip link set "$va" netns "$pa" && ip link set "$vb" netns "$pb" \
        && nsenter -t "$pa" -n ip link set "$va" name "$ia" && nsenter -t "$pa" -n ip link set "$ia" up \
        && nsenter -t "$pb" -n ip link set "$vb" name "$ib" && nsenter -t "$pb" -n ip link set "$ib" up; }; then
    undo
    fail "could not wire $ca:$ia to $cb:$ib"
fi
echo "linked $ca:$ia <-> $cb:$ib"
