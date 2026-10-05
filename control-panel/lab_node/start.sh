#!/bin/sh
# Start of every container node. Takes the lab's public key from the environment (see container_lab.up) and runs sshd
# in the foreground. Lab links are not here: they don't exist yet when the container starts. The panel creates each
# link (lab_node/link.sh) and then runs lab-wire in the node.
set -e

install -d -m 700 -o bench -g bench /home/bench/.ssh
printf '%s\n' "$AG_PUBKEY" > /home/bench/.ssh/authorized_keys
chown bench:bench /home/bench/.ssh/authorized_keys
chmod 600 /home/bench/.ssh/authorized_keys

exec /usr/sbin/sshd -D -e
