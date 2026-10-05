#!/bin/sh
# Run by the control panel (docker exec) once this node's links exist: brings the lab NICs up and, for a switch,
# bridges them into one L2 domain (the same role the VM switch plays). Reads AG_LAB_LINKS and AG_ROLE from the
# environment that container was created with.
set -eu
i=1
while [ "$i" -le "${AG_LAB_LINKS:-0}" ]; do
    ip link set "eth$i" up
    i=$((i + 1))
done
if [ "${AG_ROLE:-}" = "switch" ]; then
    ip link add name br0 type bridge
    i=1
    while [ "$i" -le "${AG_LAB_LINKS:-0}" ]; do
        ip link set "eth$i" master br0
        i=$((i + 1))
    done
    ip link set br0 up
fi
