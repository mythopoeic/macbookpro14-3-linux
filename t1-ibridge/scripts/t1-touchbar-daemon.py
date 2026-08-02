#!/usr/bin/env python3
"""Userspace Touch Bar driver for the Apple T1, using only hidraw and evdev.

Why this exists
---------------
The out-of-tree apple-ibridge/apple-touchbar pair does three things: it
synthesises virtual HID sub-devices, it sets the bar's mode and display state,
and it switches layouts on Fn while dimming on idle. Probing the hardware
directly showed the first is redundant -- the T1's interface 1.2 is an ordinary
boot keyboard that hid-generic already delivers, Esc included -- and the second
is a single 11-byte HID feature report. So the only part that actually needs
writing is the third, and none of it needs to be in the kernel.

That matters because the kernel path has hard-crashed this machine twice. See
../patches/UPSTREAM.md. This daemon touches no kernel module at all.

What it does
------------
  * sets the display on at startup and after the bar reappears
  * shows `base` mode normally, `fn` mode while the Fn key is held
  * dims after idle_timeout, switches off after a further dim_timeout,
    and restores instantly on any keyboard, trackpad or Touch Bar activity

Hardware facts it relies on (all confirmed on a MacBookPro14,3, T1 05ac:8600)
----------------------------------------------------------------------------
  1-3:1.2   83 B   app 00010006   Output  report 2: 00FF0004 @byte 0   (mode)
  1-3:1.3  634 B   app FF120001   Feature report 2: FF120020 @byte 0   (aux1)
                                                    FF120021 @byte 1   (disp)

Report IDs and offsets are still discovered at runtime by parsing the report
descriptors rather than hardcoded, so a firmware change shows up as a clean
error instead of a wrong write.

Deliberately self-contained: it duplicates the small HID parser from
touchbar-hidraw-probe.py so it can be installed to /usr/local/sbin on its own,
with no import path to get wrong.
"""

import argparse
import array
import errno
import fcntl
import glob
import os
import re
import select
import struct
import sys
import time

# --- hidraw ioctls ---------------------------------------------------------
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (0x48 << 8) | nr


HIDIOCGRDESCSIZE = _ioc(_IOC_READ, 0x01, 4)
HIDIOCGRDESC = _ioc(_IOC_READ, 0x02, 4 + 4096)


def HIDIOCSFEATURE(size):
    return _ioc(_IOC_READ | _IOC_WRITE, 0x06, size)


# --- usages ----------------------------------------------------------------
HID_UP_APPLE = 0xFF120000
USAGE_DISP = HID_UP_APPLE | 0x0021
USAGE_DISP_AUX1 = HID_UP_APPLE | 0x0020
HID_GD_KEYBOARD = 0x00010006
USAGE_MODE = 0x00FF0004

MODE_VALUES = {"esc": 0, "fn": 1, "spcl": 2, "off": 3}
DISP_ON, DISP_DIM, DISP_OFF = 1, 2, 4

# --- evdev -----------------------------------------------------------------
EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
KEY_FN = 464
EVENT_FMT = "@llHHi"                     # timeval + type + code + value
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# struct input_keymap_entry: flags, len, index, keycode, scancode[32]
KEYMAP_FMT = "=BBHI32s"
KEYMAP_SIZE = struct.calcsize(KEYMAP_FMT)          # 40
INPUT_KEYMAP_BY_INDEX = 1 << 0


def _eioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (0x45 << 8) | nr   # 'E'


EVIOCGKEYCODE_V2 = _eioc(_IOC_READ, 0x04, KEYMAP_SIZE)
EVIOCSKEYCODE_V2 = _eioc(_IOC_WRITE, 0x04, KEYMAP_SIZE)

# The T1 sends plain F1-F12 usages whatever layout it is drawing, so the host
# has to decide what they mean. This is the same table apple-touchbar applies in
# appletb_input_configured() (apple-touchbar.c:185).
#
# Measured on this firmware: the spcl strip is TEN keys, not twelve, and sends
# left to right
#
#     F1  F2  F5  F6  F7  F8  F9  F10  F11  F12
#
# F3 and F4 are absent -- the T1 does not draw Mission Control or Launchpad in
# spcl mode at all. The driver's table marks those two "not used", which turns
# out to understate it: they are unreachable, not merely unbound. They are kept
# below only so a non-spcl base layout still gets a sane mapping.
KEY_F1, KEY_F10, KEY_F11, KEY_F12 = 59, 68, 87, 88
FKEYS = [59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88]     # F1..F12
FN_MEDIA = {
    59: 224,    # F1  -> BRIGHTNESSDOWN
    60: 225,    # F2  -> BRIGHTNESSUP
    61: 120,    # F3  -> SCALE           (Expose; may do nothing on this desktop)
    62: 204,    # F4  -> ALL_APPLICATIONS (aka KEY_DASHBOARD)
    63: 229,    # F5  -> KBDILLUMDOWN
    64: 230,    # F6  -> KBDILLUMUP
    65: 165,    # F7  -> PREVIOUSSONG
    66: 164,    # F8  -> PLAYPAUSE
    67: 163,    # F9  -> NEXTSONG
    68: 113,    # F10 -> MUTE
    87: 114,    # F11 -> VOLUMEDOWN
    88: 115,    # F12 -> VOLUMEUP
}

# Keys the daemon acts on itself, because nothing else on this machine does.
# Volume and playback are deliberately absent: the desktop handles those, and
# handling them here too would double-step.
BACKLIGHT_KEYS = {
    224: ("screen", -1),    # KEY_BRIGHTNESSDOWN
    225: ("screen", +1),    # KEY_BRIGHTNESSUP
    229: ("kbd", -1),       # KEY_KBDILLUMDOWN
    230: ("kbd", +1),       # KEY_KBDILLUMUP
}

# Devices we watch. Matched by exact name from /proc/bus/input/devices so the
# eventN numbering is free to change across boots.
KEYBOARD_NAME = "Apple SPI Keyboard"     # carries KEY_FN
ACTIVITY_NAMES = ("Apple SPI Keyboard", "Apple SPI Touchpad",
                  "Apple Inc. iBridge")


def log(msg):
    print(f"t1-touchbar: {msg}", flush=True)


# --- minimal HID report descriptor parser ----------------------------------
class Field:
    __slots__ = ("usage", "bit_offset", "bit_size", "application")

    def __init__(self, usage, bit_offset, bit_size, application):
        self.usage, self.bit_offset = usage, bit_offset
        self.bit_size, self.application = bit_size, application


def parse_rdesc(rdesc):
    reports, bits = {}, {}
    g = {"usage_page": 0, "report_size": 0, "report_count": 0, "report_id": 0}
    gstack, usages, coll = [], [], []
    usage_min = usage_max = None
    application = 0
    i, n = 0, len(rdesc)
    while i < n:
        b = rdesc[i]
        if b == 0xFE:
            if i + 1 >= n:
                break
            i += 3 + rdesc[i + 1]
            continue
        size = b & 0x03
        if size == 3:
            size = 4
        itype, tag = (b >> 2) & 0x03, (b >> 4) & 0x0F
        val = int.from_bytes(rdesc[i + 1:i + 1 + size], "little") if size else 0
        i += 1 + size

        if itype == 1:
            if tag == 0x0:
                g["usage_page"] = val
            elif tag == 0x7:
                g["report_size"] = val
            elif tag == 0x8:
                g["report_id"] = val
            elif tag == 0x9:
                g["report_count"] = val
            elif tag == 0xA:
                gstack.append(dict(g))
            elif tag == 0xB and gstack:
                g = gstack.pop()
        elif itype == 2:
            full = val if size == 4 else (g["usage_page"] << 16) | val
            if tag == 0x0:
                usages.append(full)
            elif tag == 0x1:
                usage_min = full
            elif tag == 0x2:
                usage_max = full
        elif itype == 0:
            if tag == 0xA:                       # Collection
                if val == 0x01 and usages:
                    application = usages[0]
                coll.append(val)
            elif tag == 0xC:                     # End Collection
                if coll:
                    coll.pop()
                if not coll:
                    application = 0
            elif tag in (0x8, 0x9, 0xB):         # Input / Output / Feature
                kind = {0x8: "Input", 0x9: "Output", 0xB: "Feature"}[tag]
                key = (kind, g["report_id"])
                reports.setdefault(key, [])
                off = bits.get(key, 0)
                ulist = list(usages)
                if usage_min is not None and usage_max is not None:
                    ulist += list(range(usage_min, usage_max + 1))
                for k in range(g["report_count"]):
                    u = ulist[k] if k < len(ulist) else (ulist[-1] if ulist
                                                        else 0)
                    reports[key].append(Field(u, off, g["report_size"],
                                              application))
                    off += g["report_size"]
                bits[key] = off
            usages = []
            usage_min = usage_max = None
    return reports


def report_len_bytes(fields):
    if not fields:
        return 0
    return (max(f.bit_offset + f.bit_size for f in fields) + 7) // 8


# --- the bar ---------------------------------------------------------------
class TouchBar:
    """Wraps the two hidraw nodes and the two reports we drive."""

    def __init__(self, wait=15.0):
        self.mode_path = self.disp_path = None
        self.mode_rid = self.disp_rid = None
        self.disp_len = 0
        self.disp_byte = self.aux1_byte = None
        self.discover(wait)

    def discover(self, wait=0.0):
        """Locate both nodes, retrying while udev settles.

        When started from a udev rule the hidraw nodes may not exist yet, so
        poll rather than failing the unit and relying on a restart.
        """
        deadline = time.monotonic() + wait
        while True:
            self.mode_path = self.disp_path = None
            self.__init_paths()
            if self.mode_path and self.disp_path:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Touch Bar hidraw nodes not found "
                    "(is the T1 enumerated? 'lsusb | grep 05ac' should "
                    "show 8600, not 1281)")
            time.sleep(0.5)

    def __init_paths(self):
        for path in sorted(glob.glob("/dev/hidraw*")):
            try:
                fd = os.open(path, os.O_RDWR)
            except OSError:
                continue
            try:
                size = array.array("i", [0])
                fcntl.ioctl(fd, HIDIOCGRDESCSIZE, size, True)
                buf = array.array("B", [0] * (4 + 4096))
                buf[0:4] = array.array("B", size[0].to_bytes(4, "little"))
                fcntl.ioctl(fd, HIDIOCGRDESC, buf, True)
                rdesc = bytes(buf[4:4 + size[0]])
            except OSError:
                continue
            finally:
                os.close(fd)

            reports = parse_rdesc(rdesc)
            for (kind, rid), fl in reports.items():
                if kind == "Output" and any(
                        f.usage == USAGE_MODE
                        and f.application == HID_GD_KEYBOARD for f in fl):
                    self.mode_path, self.mode_rid = path, rid
                if kind == "Feature":
                    d = next((f for f in fl if f.usage == USAGE_DISP), None)
                    a = next((f for f in fl if f.usage == USAGE_DISP_AUX1),
                             None)
                    if d and a and not (d.bit_offset % 8 or a.bit_offset % 8):
                        self.disp_path, self.disp_rid = path, rid
                        self.disp_len = 1 + report_len_bytes(fl)
                        self.disp_byte = 1 + d.bit_offset // 8
                        self.aux1_byte = 1 + a.bit_offset // 8

    def set_mode(self, mode):
        fd = os.open(self.mode_path, os.O_RDWR)
        try:
            os.write(fd, bytes([self.mode_rid, MODE_VALUES[mode]]))
        finally:
            os.close(fd)

    def set_display(self, state):
        """state: DISP_ON / DISP_DIM / DISP_OFF."""
        fd = os.open(self.disp_path, os.O_RDWR)
        try:
            out = array.array("B", [0] * self.disp_len)
            out[0] = self.disp_rid
            out[self.aux1_byte] = 1
            out[self.disp_byte] = state
            fcntl.ioctl(fd, HIDIOCSFEATURE(len(out)), out, True)
        finally:
            os.close(fd)


# --- evdev discovery -------------------------------------------------------
def find_event_nodes(names):
    """Map device name -> /dev/input/eventN, by name not by number."""
    found = {}
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return found
    for blk in blocks:
        m = re.search(r'N: Name="([^"]*)"', blk)
        h = re.search(r"H: Handlers=(.*)", blk)
        if not m or not h or m.group(1) not in names:
            continue
        for tok in h.group(1).split():
            if tok.startswith("event"):
                found[m.group(1)] = f"/dev/input/{tok}"
                break
    return found


class Backlight:
    """A sysfs brightness control, stepped in 5% increments.

    Why the daemon does this itself rather than leaving it to the desktop:
    on this machine the panel is `gmux_backlight` with type=platform, and
    GNOME does not drive it -- volume and playback keys work, so the events
    clearly arrive, they just have nothing acting on them. The daemon already
    runs as root and already sees the keys, so handling it here works
    regardless of desktop, session type or compositor.
    """

    def __init__(self, path, floor=0):
        self.path = os.path.join(path, "brightness")
        with open(os.path.join(path, "max_brightness")) as f:
            self.max = int(f.read().strip())
        self.floor = floor
        self.step = max(1, self.max // 20)
        self.last_written = None

    def read(self):
        with open(self.path) as f:
            return int(f.read().strip())

    def adjust(self, direction, debug=False):
        try:
            before = self.read()
            # Compare against what WE last wrote, not against an immediate
            # re-read. A competing controller (gsd-power's ambient/auto
            # brightness) reverts the value between keypresses, not within
            # microseconds, so an immediate re-read always looks clean.
            expected = self.last_written
            value = max(self.floor, min(self.max, before + direction * self.step))
            with open(self.path, "w") as f:
                f.write(str(value))
            self.last_written = value
            if debug:
                name = os.path.basename(os.path.dirname(self.path))
                note = ""
                if expected is not None and before != expected:
                    note = (f"  <-- we last wrote {expected} but it now reads "
                            f"{before}; something else is driving this")
                log(f"backlight {name}: {before} {direction:+d}*{self.step} "
                    f"-> {value}{note}")
            return value
        except OSError as e:
            log(f"backlight {self.path}: {e}")
            return None


def find_backlight():
    """Screen backlight. Never allow 0 -- that can black the panel outright."""
    for path in sorted(glob.glob("/sys/class/backlight/*")):
        try:
            return Backlight(path, floor=1)
        except OSError:
            continue
    return None


def find_kbd_backlight():
    for path in sorted(glob.glob("/sys/class/leds/*kbd_backlight*")):
        try:
            return Backlight(path, floor=0)
        except OSError:
            continue
    return None


class FnKeymap:
    """Retargets the bar's F1-F12 between function keys and media keys.

    The T1 always reports F1-F12; only the glyphs change with the mode. So the
    translation has to happen host-side, and it has to follow the mode --
    a static hwdb rule could not, since holding Fn must still give real F-keys.

    Scancodes are discovered by walking the live keymap by index rather than
    assuming the HID usage encoding, so this keeps working if the mapping ever
    differs. EVIOCSKEYCODE also fixes up the device's key capability bits, so
    the new codes are advertised as well as emitted.
    """

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)
        self.entries = {}          # F-keycode -> (len, scancode bytes)
        self.__discover()

    def __discover(self):
        for index in range(1024):
            buf = bytearray(struct.pack(KEYMAP_FMT, INPUT_KEYMAP_BY_INDEX,
                                        0, index, 0, b""))
            try:
                fcntl.ioctl(self.fd, EVIOCGKEYCODE_V2, buf, True)
            except OSError:
                break              # EINVAL marks the end of the keymap
            _flags, length, _idx, keycode, scancode = struct.unpack(
                KEYMAP_FMT, bytes(buf))
            if keycode in FN_MEDIA and keycode not in self.entries:
                self.entries[keycode] = (length, scancode[:length])

    @property
    def complete(self):
        return len(self.entries) == len(FN_MEDIA)

    def missing(self):
        return sorted(set(FN_MEDIA) - set(self.entries))

    def apply(self, media):
        """media=True -> brightness/volume/playback; False -> plain F1-F12."""
        for fkey, (length, scancode) in self.entries.items():
            target = FN_MEDIA[fkey] if media else fkey
            buf = bytearray(struct.pack(
                KEYMAP_FMT, 0, length, 0, target,
                scancode.ljust(32, b"\0")))
            try:
                fcntl.ioctl(self.fd, EVIOCSKEYCODE_V2, buf, True)
            except OSError as e:
                log(f"keymap: setting {fkey} -> {target} failed: {e}")

    def close(self):
        try:
            self.apply(media=False)      # never leave F-keys hijacked
        finally:
            os.close(self.fd)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base-mode", default="spcl", choices=sorted(MODE_VALUES),
                    help="layout shown normally (default: spcl)")
    ap.add_argument("--fn-mode", default="fn", choices=sorted(MODE_VALUES),
                    help="layout shown while Fn is held (default: fn)")
    ap.add_argument("--idle-timeout", type=float, default=60.0,
                    help="seconds of inactivity before dimming (0 disables)")
    ap.add_argument("--dim-timeout", type=float, default=60.0,
                    help="further seconds dimmed before switching off")
    ap.add_argument("--oneshot", action="store_true",
                    help="apply --display / --base-mode once and exit")
    ap.add_argument("--display", choices=("on", "dim", "off"),
                    help="with --oneshot, the display state to set")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="seconds to wait for the hidraw nodes to appear")
    ap.add_argument("--no-media-keys", action="store_true",
                    help="leave F1-F12 as function keys in every mode, "
                         "instead of retargeting them to brightness/volume/"
                         "playback while the media layout is shown")
    ap.add_argument("--no-backlight", action="store_true",
                    help="do not act on the brightness and keyboard-illum "
                         "keys directly; leave them to the desktop")
    ap.add_argument("--debug", action="store_true",
                    help="log every key the daemon acts on, and re-read the "
                         "backlight afterwards to expose anything else "
                         "driving it")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("must run as root (hidraw and evdev nodes are root-only)")

    if args.oneshot:
        # Used by ExecStopPost= to darken the bar when the unit stops, and
        # handy by hand. Short wait: if the T1 is gone there is nothing to do.
        try:
            bar = TouchBar(wait=min(args.wait, 2.0))
        except RuntimeError as e:
            log(f"oneshot: {e}")
            return 0
        if args.display:
            bar.set_display({"on": DISP_ON, "dim": DISP_DIM,
                             "off": DISP_OFF}[args.display])
            log(f"oneshot: display={args.display}")
        else:
            bar.set_mode(args.base_mode)
            log(f"oneshot: mode={args.base_mode}")
        return 0

    bar = TouchBar(wait=args.wait)
    log(f"mode node {bar.mode_path} (output report {bar.mode_rid}), "
        f"display node {bar.disp_path} (feature report {bar.disp_rid})")

    nodes = find_event_nodes(set(ACTIVITY_NAMES))
    if KEYBOARD_NAME not in nodes:
        log(f"warning: {KEYBOARD_NAME!r} not found; Fn switching disabled")
    for name, path in sorted(nodes.items()):
        log(f"watching {path}  ({name})")

    fds = {}
    for name, path in nodes.items():
        try:
            # read-only and never EVIOCGRAB, so normal input is untouched
            fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = name
        except OSError as e:
            log(f"cannot open {path}: {e}")

    if not fds:
        sys.exit("no input devices to watch")

    # The bar reports F1-F12 whatever it is drawing, so retarget them to match
    # the layout on screen. Only meaningful when the base layout is not itself
    # the function keys.
    keymap = None
    if not args.no_media_keys and args.base_mode != "fn":
        tb_path = nodes.get("Apple Inc. iBridge")
        if not tb_path:
            log("warning: Touch Bar evdev node not found; "
                "F-keys will not be retargeted")
        else:
            try:
                keymap = FnKeymap(tb_path)
            except OSError as e:
                log(f"warning: cannot open {tb_path} for keymap: {e}")
            else:
                if not keymap.complete:
                    log(f"warning: only mapped {len(keymap.entries)}/"
                        f"{len(FN_MEDIA)} F-keys "
                        f"(missing {keymap.missing()})")
                log(f"retargeting {len(keymap.entries)} F-keys to media keys "
                    f"while showing {args.base_mode}")

    screen = kbd = None
    if not args.no_backlight:
        screen, kbd = find_backlight(), find_kbd_backlight()
        if screen:
            log(f"screen backlight {screen.path} "
                f"(0-{screen.max}, step {screen.step})")
        else:
            log("no screen backlight found; brightness keys left to the desktop")
        if kbd:
            log(f"keyboard backlight {kbd.path} "
                f"(0-{kbd.max}, step {kbd.step})")

    try:
        return run(args, bar, fds, keymap, screen, kbd)
    finally:
        if keymap:
            keymap.close()


def run(args, bar, fds, keymap, screen, kbd):
    poller = select.poll()
    for fd in fds:
        poller.register(fd, select.POLLIN)

    fn_held = False
    want_mode = args.base_mode
    display = DISP_ON
    last_activity = time.monotonic()

    bar.set_display(DISP_ON)
    bar.set_mode(want_mode)
    if keymap:
        keymap.apply(media=True)
    log(f"ready: base={args.base_mode} fn={args.fn_mode} "
        f"idle={args.idle_timeout}s dim={args.dim_timeout}s"
        f"{' media-keys' if keymap else ''}")

    while True:
        # If every watched device has gone (T1 unplugged or USB-reset), exit
        # non-zero so systemd restarts us and udev re-triggers when it returns.
        # Without this the loop would sit in poll() with nothing registered --
        # and once the display had timed out to off there is no timeout either,
        # so it would block forever instead of recovering.
        if not fds:
            log("all watched input devices went away; exiting to be restarted")
            return 1

        # wake up exactly when the next timeout would fire
        timeout_ms = None
        if args.idle_timeout > 0:
            idle = time.monotonic() - last_activity
            if display == DISP_ON:
                timeout_ms = max(0, (args.idle_timeout - idle) * 1000)
            elif display == DISP_DIM:
                deadline = args.idle_timeout + args.dim_timeout
                timeout_ms = max(0, (deadline - idle) * 1000)

        events = poller.poll(timeout_ms)
        activity = False

        for fd, _flags in events:
            try:
                data = os.read(fd, EVENT_SIZE * 64)
            except BlockingIOError:
                continue
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EIO):
                    log(f"{fds[fd]} went away; unregistering")
                    poller.unregister(fd)
                    os.close(fd)
                    del fds[fd]
                    continue
                raise

            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _s, _us, etype, code, value = struct.unpack_from(
                    EVENT_FMT, data, off)
                if etype == EV_SYN:
                    continue
                if etype in (EV_KEY, EV_REL, EV_ABS):
                    activity = True
                if etype == EV_KEY and code == KEY_FN and fds[fd] == \
                        KEYBOARD_NAME:
                    # value 1 = press, 2 = autorepeat, 0 = release
                    fn_held = value != 0
                elif etype == EV_KEY and value in (1, 2):
                    # value 2 is autorepeat, so holding the key ramps
                    target, direction = BACKLIGHT_KEYS.get(code, (None, 0))
                    # Log ONLY the keys we act on. Logging every keycode would
                    # write everything typed on the internal keyboard to the
                    # journal -- a keylogger by accident.
                    if args.debug and target:
                        log(f"key {code} from {fds[fd]} (value {value})")
                    dev = screen if target == "screen" else \
                        kbd if target == "kbd" else None
                    if dev:
                        dev.adjust(direction, debug=args.debug)

        new_mode = args.fn_mode if fn_held else args.base_mode
        if new_mode != want_mode:
            want_mode = new_mode
            try:
                bar.set_mode(want_mode)
            except OSError as e:
                log(f"mode write failed: {e}")
            # Follow the glyphs: real F-keys while Fn shows them, media keys
            # otherwise. Done after the mode write so the two never disagree
            # for longer than one iteration.
            if keymap:
                keymap.apply(media=not fn_held)

        if activity:
            last_activity = time.monotonic()
            if display != DISP_ON:
                display = DISP_ON
                try:
                    bar.set_display(DISP_ON)
                    # the T1 can forget the layout while dark
                    bar.set_mode(want_mode)
                except OSError as e:
                    log(f"wake failed: {e}")
        elif args.idle_timeout > 0:
            idle = time.monotonic() - last_activity
            if display == DISP_ON and idle >= args.idle_timeout:
                display = DISP_DIM
                try:
                    bar.set_display(DISP_DIM)
                except OSError as e:
                    log(f"dim failed: {e}")
            elif display == DISP_DIM and \
                    idle >= args.idle_timeout + args.dim_timeout:
                display = DISP_OFF
                try:
                    bar.set_display(DISP_OFF)
                except OSError as e:
                    log(f"off failed: {e}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        sys.exit(f"t1-touchbar: {e}")
