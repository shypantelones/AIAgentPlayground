#!/bin/sh
# Installs the two root helpers the control panel uses for container labs, and the one sudoers line that lets the panel
# user run them without a password. Run once, as root, on the Linux machine that runs the panel:
#   sudo sh install-helpers.sh [panel user]          (the panel user defaults to the user who ran sudo)
# Each helper is copied root-owned, so the panel user can't edit it into a root shell. The helpers check their own
# arguments; the sudoers line allows only these two paths.
# DEST_DIR and SUDOERS_DIR override the install locations (used to test this script without root).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
user=${1:-${SUDO_USER:-}}
dest=${DEST_DIR:-/usr/local/sbin}
sudoers_dir=${SUDOERS_DIR:-/etc/sudoers.d}
[ -n "$user" ] || { echo "name the panel user: sudo sh install-helpers.sh <user>" >&2; exit 2; }
if [ -z "${DEST_DIR:-}" ] && [ "$(id -u)" -ne 0 ]; then echo "run this as root (sudo)" >&2; exit 2; fi

owner="-o root -g root"
[ -z "${DEST_DIR:-}" ] || owner=""                 # a test directory isn't owned by root; real installs are
# shellcheck disable=SC2086
install -m 0755 $owner "$here/link.sh" "$dest/ag-lab-link"
# shellcheck disable=SC2086
install -m 0755 $owner "$here/forward.sh" "$dest/ag-lab-forward"

line="$user ALL=(root) NOPASSWD: $dest/ag-lab-link, $dest/ag-lab-forward"
tmp=$(mktemp)
printf '%s\n' "# Container labs (control panel): the panel may run these two root helpers, nothing else." > "$tmp"
printf '%s\n' "$line" >> "$tmp"
if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$tmp" >/dev/null 2>&1; then
    rm -f "$tmp"; echo "the sudoers line did not check out; nothing else was changed" >&2; exit 2
fi
# shellcheck disable=SC2086
install -m 0440 $owner "$tmp" "$sudoers_dir/ag-lab"
rm -f "$tmp"
echo "installed $dest/ag-lab-link and $dest/ag-lab-forward; sudoers line for $user in $sudoers_dir/ag-lab"
