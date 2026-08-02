#!/bin/bash
# Hand interface 1-3:1.3 (hid device .0002) to apple-ibridge.
#
# Why: the touch bar display controls live in the Apple 0xFF12 usage page,
# which is only in the 634-byte descriptor on interface 1.3. Interface 1.2 is
# just an 83-byte boot keyboard. apple-touchbar refuses to act until it has
# BOTH a mode interface and a display interface:
#     if (tb_dev->mode_iface.hdev && tb_dev->disp_iface.hdev && ...)
# We gave it only 1.2, so it has been sitting idle.
#
# This does NOT unload any module -- only a sysfs unbind/bind, which avoids
# the rmmod path that hard-locked the machine earlier.
#
# The ambient light sensor should survive: apple-ibridge will publish a
# virtual ALS device (1d6b:0302) carrying the sensor collections, and
# hid-sensor-hub should bind to that instead.
#
# To undo: REBOOT. Never rmmod.

LOG=/home/youruser/touchbar-attempt.log
say() { echo "$*" | tee -a "$LOG"; sync; }

# ---------------------------------------------------------------------------
# DISABLED 2026-07-31 -- THIS SCRIPT PANICS THE MACHINE.
#
# The bind itself succeeds. About 11 seconds later the machine panics on a NULL
# dereference in appleib_forward_int_op() (apple-ibridge.c:178), which walks
# hdev_info->sub_hdevs[] and reads sub_hdev->driver without checking for NULL.
# Interface 1.2 leaves the ALS slot NULL, and every suspend/resume path runs
# that loop, so USB runtime autosuspend reaches it on its own:
#
#   BUG: unable to handle page fault for address: 0000000000001c00
#   Workqueue: pm pm_runtime_work
#   RIP: 0010:appleib_hid_suspend+0x1f/0x70 [apple_ibridge]
#
# CORRECTION 2026-07-31, from the kdump vmcore in /var/crash/202607311851:
# this block previously blamed GFP_DMA exhaustion -- two hid-sensor-hub
# instances polling through appleib_ll_raw_request(), the DMA zone gone in ~36
# seconds, applespi failing with -ENOMEM. That was wrong. The oops is at +11.4s,
# and that boot recorded ZERO applespi errors and ZERO allocation failures.
#
# There IS a real second problem, just not a fatal one: hid-sensor-hub claims
# the virtual touch bar device (1d6b:0301) because apple-ibridge hands every
# sub-device the full 634-byte descriptor and hid_set_group() sees sensor
# collections in it. That blocks the display from working; it does not crash.
#
# Do not run this until bug 3 is fixed. See ../patches/UPSTREAM.md.
# ---------------------------------------------------------------------------
echo "DISABLED: this script panics the machine ~11s after the bind (bug 3,"
echo "NULL deref in appleib_forward_int_op via USB autosuspend)."
echo "See the comment block at the top of this file for the reason."
exit 1

[ "$(id -u)" = 0 ] || { echo "run me with sudo"; exit 1; }

say ""
say "=== binding interface 1.3: $(date) ==="

# --- Guards ----------------------------------------------------------------
if ! lsmod | grep -q apple_ibridge; then
    say "ABORT: apple-ibridge is not loaded. Run attempt-touchbar.sh first."
    exit 1
fi
if ! lsmod | grep -q apple_touchbar; then
    say "ABORT: apple-touchbar is not loaded. Run attempt-touchbar.sh first."
    exit 1
fi
PRE=$(journalctl -k -b 0 --no-pager | grep -icE 'UBSAN|BUG:|Oops')
if [ "$PRE" -gt 0 ]; then
    say "ABORT: this boot already shows UBSAN/BUG/Oops. Reboot first."
    exit 1
fi
say "guards passed (modules loaded, no prior UBSAN)"

DEV=0003:05AC:8600.0002
if [ ! -e "/sys/bus/hid/devices/$DEV" ]; then
    say "ABORT: $DEV not found"
    exit 1
fi

CUR=$(basename "$(readlink /sys/bus/hid/devices/$DEV/driver 2>/dev/null)" 2>/dev/null)
say "current driver on $DEV: ${CUR:-NONE}"
say "als before: $(cat /sys/bus/iio/devices/iio:device0/in_illuminance_raw 2>/dev/null || echo none)"
say ""

sync; sync
say "synced"

# --- Rebind ----------------------------------------------------------------
if [ -n "$CUR" ] && [ "$CUR" != "apple-ibridge-hid" ]; then
    say "unbinding from $CUR..."
    echo "$DEV" > "/sys/bus/hid/drivers/$CUR/unbind" 2>&1 | tee -a "$LOG"
    sync
    sleep 2
    say "unbound, still alive"
fi

say "binding to apple-ibridge-hid..."
echo "$DEV" > /sys/bus/hid/drivers/apple-ibridge-hid/bind 2>&1 | tee -a "$LOG"
sync
sleep 5
say "bind returned, still alive"
say ""

# --- Check for trouble -----------------------------------------------------
POST=$(journalctl -k -b 0 --no-pager --since "-2 min" | grep -icE 'UBSAN|BUG:|Oops|out-of-bounds|invalid-load')
say "problem lines since bind: $POST"
if [ "$POST" -gt 0 ]; then
    say "!!! trouble detected -- DO NOT rmmod, REBOOT now"
    journalctl -k -b 0 --no-pager --since "-2 min" | grep -iE 'UBSAN|BUG:|Oops|ibridge|touchbar' | tail -20 | tee -a "$LOG"
    sync
    exit 1
fi
say ""

# --- Report ----------------------------------------------------------------
say "=== HID devices now ==="
for h in /sys/bus/hid/devices/*; do
    [ -e "$h" ] || continue
    n=$(basename "$h")
    case "$n" in
        *05AC:8600*|*1D6B:03*) say "  $n  driver=$(basename "$(readlink "$h/driver" 2>/dev/null)" 2>/dev/null || echo NONE)  rdesc=$(wc -c < "$h/report_descriptor" 2>/dev/null)B" ;;
    esac
done
say ""
say "als after : $(cat /sys/bus/iio/devices/iio:device0/in_illuminance_raw 2>/dev/null || echo GONE)"
say "camera    : $(ls /dev/video* 2>/dev/null | tr '\n' ' ')"
say ""
say "=== kernel messages ==="
journalctl -k -b 0 --no-pager --since "-2 min" | grep -iE 'ibridge|touchbar|appleib|appletb' | tail -20 | tee -a "$LOG"
sync

say ""
say "============================================"
say "  LOOK AT THE TOUCH BAR. Then try holding Fn."
say "  To undo: REBOOT. Do not run rmmod."
say "============================================"
