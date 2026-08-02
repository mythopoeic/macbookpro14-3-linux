#!/bin/bash
# Stop usbmuxd from confiscating the Apple T1 (iBridge, 05ac:8600).
#
# Ubuntu ships /lib/udev/rules.d/39-usbmuxd.rules, which matches both iOS
# devices (5ac/12xx) AND the iBridge (5ac/8600), then sets
# bConfigurationValue=0 to hand the device to usbmuxd over libusb.
# That unconfigures the T1 and evicts the kernel drivers we actually want
# (uvcvideo for the FaceTime camera, HID for the Touch Bar).
#
# Fix: an override with the same filename in /etc/udev/rules.d/ takes
# precedence over /lib. Identical to the original, minus the 8600 pattern,
# so iPhone/iPad tethering via usbmuxd keeps working.

set -e
[ "$(id -u)" = 0 ] || { echo "run me with sudo"; exit 1; }

DEV=/sys/bus/usb/devices/1-3

echo "=== BEFORE ==="
echo "bConfigurationValue: '$(cat $DEV/bConfigurationValue 2>/dev/null)'"
echo "usbmuxd: $(systemctl is-active usbmuxd)"
echo

cat > /etc/udev/rules.d/39-usbmuxd.rules <<'EOF'
# Local override of /lib/udev/rules.d/39-usbmuxd.rules
#
# Same rules, with the Apple T1/iBridge pattern (5ac/8600) removed.
# The packaged rule unconfigures the iBridge (bConfigurationValue=0) and
# hands it to usbmuxd, which prevents the kernel from driving the built-in
# FaceTime camera and Touch Bar. iOS devices (5ac/12xx) are unaffected.

SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ENV{PRODUCT}=="5ac/12[9a][0-9a-f]/*", TAG+="systemd"

SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ENV{PRODUCT}=="5ac/12[9a][0-9a-f]/*", ACTION=="add", ENV{USBMUX_SUPPORTED}="1", ATTR{bConfigurationValue}="0", OWNER="usbmux", ENV{SYSTEMD_WANTS}="usbmuxd.service"

SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ENV{PRODUCT}=="5ac/12[9a][0-9a-f]/*", ACTION=="bind", ENV{USBMUX_SUPPORTED}="1", OWNER="usbmux", ENV{SYSTEMD_WANTS}="usbmuxd.service"

SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ENV{PRODUCT}=="5ac/12[9a][0-9a-f]/*", ACTION=="remove", RUN+="/usr/sbin/usbmuxd -x"
EOF

echo "wrote /etc/udev/rules.d/39-usbmuxd.rules"
udevadm control --reload-rules
echo "udev rules reloaded"

# usbmuxd is socket/udev-activated and 'static'; stopping it is safe. It will
# come back on its own the next time an iPhone or iPad is plugged in.
systemctl stop usbmuxd 2>/dev/null || true
sleep 1
echo "usbmuxd now: $(systemctl is-active usbmuxd)"
echo

# Force a clean re-enumeration so the add event is reprocessed under the new
# rules and the kernel selects a configuration again.
echo "re-enumerating the iBridge..."
echo 0 > $DEV/authorized
sleep 2
echo 1 > $DEV/authorized
sleep 6

echo
echo "=== AFTER ==="
echo "bConfigurationValue: '$(cat $DEV/bConfigurationValue 2>/dev/null)'"
echo
echo "--- interfaces and their drivers ---"
found=0
for i in $DEV/1-3:*; do
    [ -e "$i" ] || continue
    found=1
    drv=$(basename "$(readlink "$i/driver" 2>/dev/null)" 2>/dev/null)
    printf "  %-12s class=%s/%s/%s  driver=%s\n" \
        "$(basename "$i")" \
        "$(cat "$i/bInterfaceClass" 2>/dev/null)" \
        "$(cat "$i/bInterfaceSubClass" 2>/dev/null)" \
        "$(cat "$i/bInterfaceProtocol" 2>/dev/null)" \
        "${drv:-NONE}"
done
[ "$found" = 1 ] || echo "  (still no interfaces)"

echo
echo "--- camera ---"
ls -l /dev/video* 2>&1
echo
echo "--- hid ---"
ls -l /dev/hidraw* 2>&1
ls /sys/bus/hid/devices/ 2>/dev/null
echo
echo "--- kernel log, last 25 lines mentioning the device ---"
journalctl -k --since "-2 min" --no-pager | grep -iE 'usb 1-3|uvc|hid|video|ibridge' | tail -25

echo
echo "=== waiting 45s to confirm it stays configured ==="
sleep 45
echo "bConfigurationValue: '$(cat $DEV/bConfigurationValue 2>/dev/null)'"
echo "usbmuxd: $(systemctl is-active usbmuxd)"
ls /dev/video* 2>&1
