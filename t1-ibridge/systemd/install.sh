#!/bin/bash
# Installs the userspace Touch Bar daemon. Run as root.
#
# This replaces the out-of-tree apple-ibridge/apple-touchbar kernel modules for
# everyday use: the T1's interface 1.2 is already an ordinary boot keyboard
# that hid-generic delivers (Esc included), so the only thing missing was
# setting the mode and display, which this does over hidraw.
#
# Nothing here loads a kernel module. See ../patches/UPSTREAM.md.
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$(cd "$SRC/../scripts" && pwd)"

for f in "$SCRIPTS/t1-touchbar-daemon.py" \
         "$SRC/t1-touchbar.service" "$SRC/99-t1-touchbar.rules"; do
    if [ ! -f "$f" ]; then
        echo "error: $f not found" >&2
        exit 1
    fi
done

if [ "$(id -u)" != 0 ]; then
    echo "run me with sudo" >&2
    exit 1
fi

# The udev override that stops usbmuxd confiscating the T1 is a hard
# prerequisite: without it the device has no interfaces and no hidraw nodes.
if [ ! -f /etc/udev/rules.d/39-usbmuxd.rules ]; then
    echo "warning: /etc/udev/rules.d/39-usbmuxd.rules is missing." >&2
    echo "         Run ../scripts/fix-ibridge-usbmuxd.sh first, or the T1" >&2
    echo "         will be unconfigured by usbmuxd and this will not work." >&2
fi

install -o root -g root -m 755 "$SCRIPTS/t1-touchbar-daemon.py" \
    /usr/local/sbin/t1-touchbar-daemon
echo "installed /usr/local/sbin/t1-touchbar-daemon"

install -o root -g root -m 644 "$SRC/t1-touchbar.service" \
    /etc/systemd/system/t1-touchbar.service
echo "installed /etc/systemd/system/t1-touchbar.service"

install -o root -g root -m 644 "$SRC/99-t1-touchbar.rules" \
    /etc/udev/rules.d/99-t1-touchbar.rules
echo "installed /etc/udev/rules.d/99-t1-touchbar.rules"

systemctl daemon-reload
udevadm control --reload-rules
echo "reloaded systemd and udev"

echo
echo "--- verification ---"
python3 -m py_compile /usr/local/sbin/t1-touchbar-daemon &&
    echo "daemon: syntax OK"
rm -rf /usr/local/sbin/__pycache__
systemd-analyze verify /etc/systemd/system/t1-touchbar.service 2>&1 |
    grep -v '^$' || echo "unit file: OK"
udevadm verify /etc/udev/rules.d/99-t1-touchbar.rules 2>&1 | tail -3

echo
echo "Start it now with:  systemctl start t1-touchbar"
echo "Enable at boot:     systemctl enable t1-touchbar"
echo "Watch it:           journalctl -fu t1-touchbar"
