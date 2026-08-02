#!/bin/bash
# Installs automatic dock SuperSpeed recovery and per-dock monitor layout
# profiles. Run as root.
#
# Two halves, and they install differently:
#   * the SuperSpeed recovery is a system service, triggered from udev
#   * dock-monitor-profiles talks to mutter over the session bus, so it has to
#     be a *user* unit inside the graphical session
#
# SRC used to be hardcoded to /home/youruser/dock-hotplug. The files now live
# in this repo, so it follows the script instead — the paths that stay
# hardcoded are the destinations, which are genuinely single-machine.
set -e

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_HOME="/home/youruser"
OWNER="youruser"
OWNER_UID="$(id -u "$OWNER")"

for f in dock-xhci-recover dock-superspeed-check.service \
         80-dock-superspeed.rules 81-left-thunderbolt-xhci.rules recover-displays \
         dock-monitor-profiles.py dock-monitor-profiles.service \
         wireplumber-restart.conf; do
    if [ ! -f "$SRC/$f" ]; then
        echo "error: $SRC/$f not found" >&2
        exit 1
    fi
done

# --- system half: SuperSpeed recovery -------------------------------------

install -o root -g root -m 755 "$SRC/dock-xhci-recover" \
    /usr/local/sbin/dock-xhci-recover
echo "installed /usr/local/sbin/dock-xhci-recover"

install -o root -g root -m 644 "$SRC/dock-superspeed-check.service" \
    /etc/systemd/system/dock-superspeed-check.service
echo "installed /etc/systemd/system/dock-superspeed-check.service"

install -o root -g root -m 644 "$SRC/80-dock-superspeed.rules" \
    /etc/udev/rules.d/80-dock-superspeed.rules
echo "installed /etc/udev/rules.d/80-dock-superspeed.rules"

# Not dock-related: pins the LEFT-hand Thunderbolt xHCI out of runtime suspend.
# Its PME wake path is broken, so at control=auto it sleeps through insertions
# and the left ports look dead. See the README.
install -o root -g root -m 644 "$SRC/81-left-thunderbolt-xhci.rules" \
    /etc/udev/rules.d/81-left-thunderbolt-xhci.rules
echo "installed /etc/udev/rules.d/81-left-thunderbolt-xhci.rules"

# Replace the manual script, keeping the original once.
if [ -f "$USER_HOME/.local/bin/recover-displays" ] &&
   [ ! -f "$USER_HOME/.local/bin/recover-displays.bak" ]; then
    cp -a "$USER_HOME/.local/bin/recover-displays" \
          "$USER_HOME/.local/bin/recover-displays.bak"
    echo "backed up old recover-displays -> recover-displays.bak"
fi

install -o "$OWNER" -g "$OWNER" -m 755 "$SRC/recover-displays" \
    "$USER_HOME/.local/bin/recover-displays"
echo "installed $USER_HOME/.local/bin/recover-displays"

# --- user half: monitor layout profiles ------------------------------------

install -o "$OWNER" -g "$OWNER" -m 755 "$SRC/dock-monitor-profiles.py" \
    "$USER_HOME/.local/bin/dock-monitor-profiles"
echo "installed $USER_HOME/.local/bin/dock-monitor-profiles"

install -d -o "$OWNER" -g "$OWNER" -m 755 "$USER_HOME/.config/systemd/user"
install -o "$OWNER" -g "$OWNER" -m 644 "$SRC/dock-monitor-profiles.service" \
    "$USER_HOME/.config/systemd/user/dock-monitor-profiles.service"
echo "installed $USER_HOME/.config/systemd/user/dock-monitor-profiles.service"

# --- user half: keep WirePlumber alive -------------------------------------
#
# A dock re-plug yanks the dock's USB audio card out from under WirePlumber.
# That is survivable; WirePlumber ending is not, because the stock unit is
# Restart=on-failure and a clean exit leaves PipeWire running with no session
# manager and therefore no audio devices at all. See the README.
#
# No restart here on purpose: daemon-reload is enough for the new Restart=
# policy to apply to the already-running instance, and restarting would drop
# audio for no reason.

install -d -o "$OWNER" -g "$OWNER" -m 755 \
    "$USER_HOME/.config/systemd/user/wireplumber.service.d"
install -o "$OWNER" -g "$OWNER" -m 644 "$SRC/wireplumber-restart.conf" \
    "$USER_HOME/.config/systemd/user/wireplumber.service.d/override.conf"
echo "installed $USER_HOME/.config/systemd/user/wireplumber.service.d/override.conf"

systemctl daemon-reload
udevadm control --reload-rules
echo "reloaded systemd and udev"

# The user manager is a different systemd instance and needs its own reload
# plus a bus to talk to, so this only works while the session is up.
run_as_user() {
    runuser -u "$OWNER" -- \
        env XDG_RUNTIME_DIR="/run/user/$OWNER_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$OWNER_UID/bus" \
            "$@"
}

if [ -d "/run/user/$OWNER_UID" ]; then
    run_as_user systemctl --user daemon-reload

    # monitor-order.service ran watch-monitor-order.py -> fix-monitor-order.py,
    # which forced primary onto the built-in panel on every monitor change.
    # It fights the new daemon directly, so it has to go.
    if run_as_user systemctl --user list-unit-files monitor-order.service \
         >/dev/null 2>&1; then
        run_as_user systemctl --user disable --now monitor-order.service \
            >/dev/null 2>&1 || true
        echo "disabled the superseded monitor-order.service"
    fi

    run_as_user systemctl --user enable --now dock-monitor-profiles.service
    echo "enabled dock-monitor-profiles.service"
else
    echo "no session for $OWNER; enable the user unit yourself with:"
    echo "  systemctl --user enable --now dock-monitor-profiles.service"
    echo "  systemctl --user disable --now monitor-order.service"
fi

echo
echo "--- verification ---"
bash -n /usr/local/sbin/dock-xhci-recover && echo "recovery script: syntax OK"
bash -n "$USER_HOME/.local/bin/recover-displays" && echo "recover-displays: syntax OK"
python3 -m py_compile "$USER_HOME/.local/bin/dock-monitor-profiles" &&
    echo "dock-monitor-profiles: syntax OK"
systemd-analyze verify /etc/systemd/system/dock-superspeed-check.service \
    2>&1 | grep -v '^$' || echo "dock-superspeed-check.service: OK"
systemd-analyze --user verify \
    "$USER_HOME/.config/systemd/user/dock-monitor-profiles.service" \
    2>&1 | grep -v '^$' || echo "dock-monitor-profiles.service: OK"
udevadm verify /etc/udev/rules.d/80-dock-superspeed.rules 2>&1 | tail -3
udevadm verify /etc/udev/rules.d/81-left-thunderbolt-xhci.rules 2>&1 | tail -2

# Ask the user manager what it actually resolved to. That is a stronger check
# than a syntax verify: it proves the drop-in was found and merged, which a
# standalone file check cannot show.
if [ -d "/run/user/$OWNER_UID" ]; then
    wp_restart="$(run_as_user systemctl --user show wireplumber.service \
        -p Restart --value 2>/dev/null || true)"
    if [ "$wp_restart" = "always" ]; then
        echo "wireplumber drop-in: OK (Restart=always)"
    else
        echo "wireplumber drop-in: NOT APPLIED (Restart=${wp_restart:-unknown})" >&2
    fi
fi

echo
echo "Installed."
echo "SuperSpeed recovery runs when the dock's USB 2.0 hub appears."
echo "Layout profiles are stored per dock+monitor combination in"
echo "  $USER_HOME/.config/dock-monitor-profiles/"
echo "Arrange the displays in Settings, then:  dock-monitor-profiles save"
