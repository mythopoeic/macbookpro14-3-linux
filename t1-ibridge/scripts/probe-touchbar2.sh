#!/bin/bash
# Unbuffered Touch Bar input probe.
#
# Replaces the first version, which piped `od` into `head` -- od block-buffers,
# so a handful of 24-byte events never flushed before timeout killed it. That
# produced no output regardless of whether the Touch Bar was sending anything.
#
# This reads the evdev node AND the raw HID node directly, printing each report
# the moment it arrives, and ends with an explicit count.

[ "$(id -u)" = 0 ] || { echo "run me with sudo"; exit 1; }

EV=$(grep -A5 -i 'Vendor=05ac Product=8600' /proc/bus/input/devices \
     | grep -o 'event[0-9]*' | head -1)
[ -n "$EV" ] || { echo "could not find the iBridge input device"; exit 1; }

echo "watching /dev/input/$EV  and  /dev/hidraw0"
echo
echo "TOUCH AND SWIPE THE FULL LENGTH OF THE TOUCH BAR NOW."
echo "Far left (Esc), across the middle (F-keys), far right. 25 seconds."
echo

python3 - "/dev/input/$EV" <<'PYEOF'
import os, select, struct, sys, time

# struct input_event on 64-bit: 2x long (timeval) + u16 type + u16 code + s32 value
ESIZE = 24
paths = [sys.argv[1], "/dev/hidraw0", "/dev/hidraw1"]

fds = {}
for p in paths:
    try:
        fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = p
    except OSError as e:
        print(f"  (could not open {p}: {e})")

if not fds:
    print("no readable devices"); sys.exit(1)

EV_KEY = 0x01
deadline = time.monotonic() + 25
count = 0

while time.monotonic() < deadline:
    r, _, _ = select.select(list(fds), [], [], 0.5)
    for fd in r:
        try:
            data = os.read(fd, 4096)
        except OSError:
            continue
        if not data:
            continue
        name = fds[fd]
        if name.startswith("/dev/input"):
            for i in range(0, len(data) - ESIZE + 1, ESIZE):
                _s, _us, etype, code, value = struct.unpack("llHHi", data[i:i+ESIZE])
                if etype == 0:          # EV_SYN, skip the noise
                    continue
                count += 1
                kind = "KEY" if etype == EV_KEY else f"type{etype}"
                print(f"  [evdev] {kind} code={code} value={value}", flush=True)
        else:
            count += 1
            print(f"  [{name}] {data.hex(' ')}", flush=True)

print()
print(f"=== {count} events received ===")
if count == 0:
    print("The Touch Bar is sending nothing at all -- display off and touch")
    print("digitizer inactive. It needs a driver to send the mode-set report.")
else:
    print("The Touch Bar IS sending input while dark.")
PYEOF
