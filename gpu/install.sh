#!/bin/bash
# Installs the discrete-GPU idle governor. Run as root.
#
# Unlike the dock's user unit, this is a system service: the knobs it writes
# live under /sys/bus/pci/drivers/amdgpu and are root-owned.
#
# The service is enabled here because installing it is already a deliberate act.
# What it does NOT do is autoload from udev or modules-load.d -- the GPU is
# never touched except by this one explicitly-enabled unit.
set -e

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in amdgpu-governor amdgpu-governor.service; do
    if [ ! -f "$SRC/$f" ]; then
        echo "error: $SRC/$f not found" >&2
        exit 1
    fi
done

# Install via a temp file plus rename, never in place. rename(2) is atomic and
# leaves the running daemon holding its old inode; writing over the file
# directly is genuinely unsafe, because bash reads a script incrementally as it
# executes and would start reading the new bytes at a stale offset.
install -o root -g root -m 755 "$SRC/amdgpu-governor" \
    /usr/local/sbin/.amdgpu-governor.new
mv -f /usr/local/sbin/.amdgpu-governor.new /usr/local/sbin/amdgpu-governor
echo "installed /usr/local/sbin/amdgpu-governor"

install -o root -g root -m 644 "$SRC/amdgpu-governor.service" \
    /etc/systemd/system/amdgpu-governor.service
echo "installed /etc/systemd/system/amdgpu-governor.service"

systemctl daemon-reload
echo "reloaded systemd"

# enable --now is NOT enough on an upgrade: it does nothing when the unit is
# already active, so the daemon keeps running the previous script. Restart
# explicitly, or an install silently has no effect.
systemctl enable amdgpu-governor.service >/dev/null 2>&1
systemctl restart amdgpu-governor.service
echo "enabled and restarted amdgpu-governor.service"

echo
echo "--- verification ---"
bash -n /usr/local/sbin/amdgpu-governor && echo "governor script: syntax OK"
systemd-analyze verify /etc/systemd/system/amdgpu-governor.service \
    2>&1 | grep -v '^$' || echo "amdgpu-governor.service: OK"

# Prove the unit actually resolved a device and is not sitting in a restart
# loop -- a syntax check cannot show either.
sleep 2
state="$(systemctl is-active amdgpu-governor.service 2>/dev/null || true)"
if [ "$state" = "active" ]; then
    echo "amdgpu-governor.service: active"
    /usr/local/sbin/amdgpu-governor status
else
    echo "amdgpu-governor.service: NOT ACTIVE ($state)" >&2
    journalctl -u amdgpu-governor.service -n 10 --no-pager >&2 || true
fi

echo
echo "Installed."
echo "The GPU is clamped to 214 MHz after 30 s idle and released instantly on"
echo "load or when Steam/a game appears. Check with:"
echo "  amdgpu-governor status"
echo "  journalctl -u amdgpu-governor -f"
echo
echo "To stop and restore the stock behaviour:"
echo "  systemctl disable --now amdgpu-governor    # restores 'auto' on stop"
