#!/bin/bash
# Careful single attempt at loading the patched T1 Touch Bar driver.
#
# Differences from the previous attempt, which hard-locked the machine:
#
#  1. Refuses to run unless this boot has never loaded these modules, so we
#     can never operate on already-corrupted kernel memory.
#  2. Touches interface 1-3:1.2 only. Leaving 1.3 with hid-sensor-hub keeps the
#     ambient light sensor working and halves the exposure.
#
#     CORRECTION (was wrong here originally): 1.2 does NOT carry all three touch
#     bar usages. Its descriptor is 83 bytes and holds only the keyboard
#     collection, 0x00010006. The other two touch bar usages -- 0x000D0005 and
#     0xFF120001, the latter being the display controls apple-touchbar needs for
#     disp_iface -- are in the 634-byte descriptor on 1.3, alongside the sensor
#     collections. That is why binding 1.2 alone can never light the display.
#
#  2b. WARNING, unfixed: binding 1.2 alone leaves sub_hdevs[1] (ALS) NULL, and
#     appleib_forward_int_op() (apple-ibridge.c:178) dereferences it without a
#     NULL check on every suspend/resume. USB runtime autosuspend reaches it with
#     no user action. Set power/control=on on the T1 and disable lid suspend
#     before running this. See patches/UPSTREAM.md, bug 3.
#  3. Writes its own log and syncs after every step. The journal never flushed
#     during the lockup; this leaves evidence on disk regardless.
#  4. Checks for UBSAN after loading apple-ibridge and ABORTS before loading
#     apple-touchbar if anything is wrong.
#  5. Never calls rmmod. If this goes wrong, REBOOT -- do not try to unload.

LOG=/home/youruser/touchbar-attempt.log

say() { echo "$*" | tee -a "$LOG"; sync; }

[ "$(id -u)" = 0 ] || { echo "run me with sudo"; exit 1; }

: > "$LOG"
say "=== Touch Bar attempt: $(date) ==="
say "boot started: $(uptime -s)"
say ""

# --- Guard 1: this boot must be clean -------------------------------------
if lsmod | grep -qE 'apple_ibridge|apple_touchbar'; then
    say "ABORT: modules are already loaded in this boot."
    say "Reboot first, then run this again."
    exit 1
fi
if journalctl -k -b 0 --no-pager | grep -qiE 'UBSAN|apple-ibridge|appleib'; then
    say "ABORT: this boot has already seen apple-ibridge activity or UBSAN."
    say "Reboot first, then run this again."
    exit 1
fi
say "guard: boot is clean, no prior module activity"

# --- Context ---------------------------------------------------------------
if lsusb | grep -q '17e9:'; then
    say "NOTE: the DisplayLink dock appears to be attached. The thunderbolt"
    say "      driver on this machine oopsed at boot earlier today. Consider"
    say "      unplugging the dock before proceeding."
fi
say "iBridge: $(lsusb | grep 05ac || echo MISSING)"
say "camera : $(ls /dev/video* 2>/dev/null | tr '\n' ' ')"
say "als    : $(cat /sys/bus/iio/devices/iio:device0/in_illuminance_raw 2>/dev/null)"
say ""

say "syncing filesystems before we touch anything..."
sync; sync
say "synced"
say ""

# --- Load apple-ibridge only ----------------------------------------------
cd /home/youruser/t1-touchbar-port || exit 1
say "STEP 1: loading apple-ibridge (patched)"
sync
insmod ./apple-ibridge.ko 2>&1 | tee -a "$LOG"
sync
sleep 4
say "STEP 1: insmod returned, still alive"
say ""

# --- Guard 2: check for corruption before going further --------------------
say "STEP 2: checking for UBSAN / oops"
PROBLEMS=$(journalctl -k -b 0 --no-pager --since "-2 min" \
           | grep -icE 'UBSAN|BUG:|Oops|out-of-bounds|invalid-load')
say "  problem lines found: $PROBLEMS"

if [ "$PROBLEMS" -gt 0 ]; then
    say ""
    say "!!! ABORTING -- the driver still misbehaves."
    say "!!! DO NOT run rmmod. REBOOT the machine now."
    say ""
    journalctl -k -b 0 --no-pager --since "-2 min" \
        | grep -iE 'UBSAN|BUG:|Oops|out-of-bounds|invalid-load|ibridge|sub-device' \
        | tail -20 | tee -a "$LOG"
    sync
    exit 1
fi
say "  clean so far"
say ""

# --- Report what sub-devices appeared --------------------------------------
say "STEP 3: virtual sub-devices created?"
for h in /sys/bus/hid/devices/*; do
    [ -e "$h" ] || continue
    say "  $(basename "$h")  driver=$(basename "$(readlink "$h/driver" 2>/dev/null)" 2>/dev/null || echo NONE)"
done
sync
say ""

if ! ls /sys/bus/hid/devices/*1D6B:0301* >/dev/null 2>&1; then
    say "No virtual touch bar device (1D6B:0301) was created."
    say "apple-touchbar has nothing to bind to, so stopping here."
    say "This is a clean stop -- no corruption. Reboot when convenient."
    journalctl -k -b 0 --no-pager --since "-2 min" | grep -iE 'ibridge|sub-device|collection' | tail -15 | tee -a "$LOG"
    sync
    exit 0
fi
say "virtual touch bar device present"
say ""

# --- Load apple-touchbar ---------------------------------------------------
say "STEP 4: loading apple-touchbar"
sync
insmod ./apple-touchbar.ko 2>&1 | tee -a "$LOG"
sync
sleep 4
say "STEP 4: insmod returned, still alive"
say ""

say "=== FINAL STATE ==="
for h in /sys/bus/hid/devices/*; do
    [ -e "$h" ] || continue
    say "  $(basename "$h")  driver=$(basename "$(readlink "$h/driver" 2>/dev/null)" 2>/dev/null || echo NONE)"
done
say "camera : $(ls /dev/video* 2>/dev/null | tr '\n' ' ')"
say "als    : $(cat /sys/bus/iio/devices/iio:device0/in_illuminance_raw 2>/dev/null)"
say ""
journalctl -k -b 0 --no-pager --since "-3 min" | grep -iE 'ibridge|touchbar|appleib|appletb|UBSAN|BUG' | tail -25 | tee -a "$LOG"
sync

say ""
say "============================================"
say "  LOOK AT THE TOUCH BAR. Try holding Fn."
say "  To undo: REBOOT. Do not run rmmod."
say "============================================"
