#!/usr/bin/env python3
"""Drive the T1 Touch Bar display directly over hidraw, with no modules loaded.

Why this exists
---------------
apple-touchbar only turns the display on once it holds BOTH a mode interface and
a display interface:

    if (tb_dev->mode_iface.hdev && tb_dev->disp_iface.hdev && ...)

The display controls (Apple usage page 0xFF12) live only in the 634-byte report
descriptor on interface 1-3:1.3. Getting that interface to apple-ibridge is what
panicked the machine twice, so before doing any of that again it is worth asking
a much cheaper question: can the panel be lit at all?

It can be asked entirely from userspace. hid-sensor-hub binds 1.3 with
HID_CONNECT_DEFAULT, which includes HID_CONNECT_HIDRAW, so /dev/hidrawN already
exists on that interface right now and coexists with the bound driver. Nothing is
loaded, nothing is unbound, and the ambient light sensor keeps working.

This script reads the report descriptor straight off the hidraw node
(HIDIOCGRDESC -- no debugfs, no usbhid-dump), decodes it, locates the feature
report carrying usage 0xFF120021 (display state) and 0xFF120020 (its required
companion field), and then does what appletb_set_tb_disp() does:

    hid_set_field(disp_field_aux1, 0, 1);
    hid_set_field(disp_field, 0, disp);      /* 1=on 2=dim 4=off */
    hid_hw_request(..., HID_REQ_SET_REPORT);

Safety
------
Read-only by default: it dumps, decodes and does a GET_FEATURE, which changes no
state. Writing requires an explicit --set. No kernel module is touched either
way, so there is no path here to the crash described in ../patches/UPSTREAM.md.

The mode report
---------------
apple-touchbar also sets a *mode* (which key layout the bar shows) on interface
1.2 before touching the display. Decoding the real descriptors confirms what that
is: interface 1.2 carries application 00010006 (HID_GD_KEYBOARD) and exactly one
mode field, usage 00FF0004, as **output report 2, one byte**. That matches the
driver's wValue=0x0202 -- (report type 2 = Output) << 8 | report ID 2 -- and
wIndex=2, the interface number.

--mode sends it as a plain hidraw output report. Note that on a T1 the driver
deliberately does not do it that way: appletb_set_tb_mode() (apple-touchbar.c:250)
falls back to a raw usb_control_msg() with USB_TYPE_VENDOR and no report-ID
prefix, which suggests the class request may be rejected. If --mode returns EPIPE
or has no effect, that quirk is why, and the fallback needs usbfs rather than
hidraw.

Confirmed layout on this machine (MacBookPro14,3, T1 05ac:8600)
---------------------------------------------------------------
    1-3:1.2   83 B   app 00010006   Output  report 2: 00FF0004 @byte 0  (mode)
    1-3:1.3  634 B   app FF120001   Feature report 2: FF120020 @byte 0  (aux1)
                                                      FF120021 @byte 1  (disp)
                     app 00200041   the ambient light sensor collections
"""

import argparse
import array
import fcntl
import os
import sys
import glob

# --- ioctl encoding --------------------------------------------------------
# _IOC(dir, type, nr, size) with dir: 1=write 2=read 3=both, type 'H' = 0x48
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (0x48 << 8) | nr


HIDIOCGRDESCSIZE = _ioc(_IOC_READ, 0x01, 4)
HIDIOCGRDESC = _ioc(_IOC_READ, 0x02, 4 + 4096)
HIDIOCGRAWINFO = _ioc(_IOC_READ, 0x03, 8)


def HIDIOCGFEATURE(size):
    return _ioc(_IOC_READ | _IOC_WRITE, 0x07, size)


def HIDIOCSFEATURE(size):
    return _ioc(_IOC_READ | _IOC_WRITE, 0x06, size)


# --- usages we care about --------------------------------------------------
HID_UP_APPLE = 0xFF120000
USAGE_APPLE_APP = HID_UP_APPLE | 0x0001   # application collection
USAGE_DISP = HID_UP_APPLE | 0x0021        # display state: 1=on 2=dim 4=off
USAGE_DISP_AUX1 = HID_UP_APPLE | 0x0020   # companion, always set to 1

HID_GD_KEYBOARD = 0x00010006              # application on interface 1.2
USAGE_MODE = 0x00FF0004                   # HID_UP_CUSTOM | 0x0004

DISP_VALUES = {"on": 1, "dim": 2, "off": 4}
# APPLETB_CMD_MODE_* from apple-touchbar.c:54
MODE_VALUES = {"esc": 0, "fn": 1, "spcl": 2, "off": 3}

MAIN_INPUT, MAIN_OUTPUT, MAIN_FEATURE = 0x8, 0x9, 0xB
MAIN_COLLECTION, MAIN_END_COLLECTION = 0xA, 0xC


class Field:
    __slots__ = ("usage", "bit_offset", "bit_size", "application")

    def __init__(self, usage, bit_offset, bit_size, application):
        self.usage = usage
        self.bit_offset = bit_offset
        self.bit_size = bit_size
        self.application = application


def parse_rdesc(rdesc):
    """Decode a HID report descriptor into {(kind, report_id): [Field, ...]}."""
    reports = {}
    bits = {}
    g = {"usage_page": 0, "report_size": 0, "report_count": 0, "report_id": 0}
    gstack = []
    usages = []
    usage_min = usage_max = None
    coll_stack = []
    application = 0

    i, n = 0, len(rdesc)
    while i < n:
        b = rdesc[i]
        if b == 0xFE:                      # long item: skip it
            if i + 1 >= n:
                break
            i += 3 + rdesc[i + 1]
            continue

        size = b & 0x03
        if size == 3:
            size = 4
        itype = (b >> 2) & 0x03
        tag = (b >> 4) & 0x0F
        data = rdesc[i + 1:i + 1 + size]
        val = int.from_bytes(data, "little") if size else 0
        i += 1 + size

        if itype == 1:                     # Global
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

        elif itype == 2:                   # Local
            full = val if size == 4 else (g["usage_page"] << 16) | val
            if tag == 0x0:
                usages.append(full)
            elif tag == 0x1:
                usage_min = full
            elif tag == 0x2:
                usage_max = full

        elif itype == 0:                   # Main
            if tag == MAIN_COLLECTION:
                # an application collection (0x01) names everything beneath it
                if val == 0x01 and usages:
                    application = usages[0]
                coll_stack.append(val)
            elif tag == MAIN_END_COLLECTION:
                if coll_stack:
                    coll_stack.pop()
                if not coll_stack:
                    application = 0
            elif tag in (MAIN_INPUT, MAIN_OUTPUT, MAIN_FEATURE):
                kind = {MAIN_INPUT: "Input",
                        MAIN_OUTPUT: "Output",
                        MAIN_FEATURE: "Feature"}[tag]
                key = (kind, g["report_id"])
                reports.setdefault(key, [])
                off = bits.get(key, 0)

                ulist = list(usages)
                if usage_min is not None and usage_max is not None:
                    ulist += list(range(usage_min, usage_max + 1))

                for k in range(g["report_count"]):
                    if k < len(ulist):
                        u = ulist[k]
                    elif ulist:
                        u = ulist[-1]      # spec: last usage repeats
                    else:
                        u = 0
                    reports[key].append(
                        Field(u, off, g["report_size"], application))
                    off += g["report_size"]

                bits[key] = off

            # any Main item clears the local state
            usages = []
            usage_min = usage_max = None

    return reports


def report_len_bytes(fields):
    """Payload length in bytes, not counting the report-ID prefix."""
    if not fields:
        return 0
    end = max(f.bit_offset + f.bit_size for f in fields)
    return (end + 7) // 8


class Node:
    __slots__ = ("path", "rdesc", "vendor", "product", "reports")

    def __init__(self, path, rdesc, vendor, product, reports):
        self.path, self.rdesc = path, rdesc
        self.vendor, self.product = vendor, product
        self.reports = reports

    def has(self, usage, application=None):
        return any(f.usage == usage
                   and (application is None or f.application == application)
                   for fl in self.reports.values() for f in fl)


def enumerate_nodes():
    """Read every hidraw node's descriptor via ioctl and decode it."""
    nodes = []
    for path in sorted(glob.glob("/dev/hidraw*")):
        try:
            fd = os.open(path, os.O_RDWR)
        except PermissionError:
            raise SystemExit(f"cannot open {path} -- run me with sudo")
        except OSError:
            continue
        try:
            size = array.array("i", [0])
            fcntl.ioctl(fd, HIDIOCGRDESCSIZE, size, True)
            buf = array.array("B", [0] * (4 + 4096))
            buf[0:4] = array.array("B", size[0].to_bytes(4, "little"))
            fcntl.ioctl(fd, HIDIOCGRDESC, buf, True)
            rdesc = bytes(buf[4:4 + size[0]])

            info = array.array("B", [0] * 8)
            fcntl.ioctl(fd, HIDIOCGRAWINFO, info, True)
            vendor = int.from_bytes(bytes(info[4:6]), "little")
            product = int.from_bytes(bytes(info[6:8]), "little")
        except OSError:
            continue
        finally:
            os.close(fd)
        nodes.append(Node(path, rdesc, vendor, product, parse_rdesc(rdesc)))
    return nodes


def find_display_node(nodes):
    for n in nodes:
        if n.has(USAGE_DISP) and n.has(USAGE_DISP_AUX1):
            return n
    raise SystemExit(
        "no hidraw node exposes both 0xFF120021 (disp) and 0xFF120020 (aux1).\n"
        "Expected one on interface 1-3:1.3 of the T1. Check that\n"
        "'lsusb | grep 05ac' shows 8600 and not 1281.")


def find_mode_node(nodes):
    for n in nodes:
        if n.has(USAGE_MODE, HID_GD_KEYBOARD):
            return n
    return None


# HID keyboard usage page (0x07) codes we care about, for --watch
KEYNAMES = {
    0x29: "ESC", 0x3A: "F1", 0x3B: "F2", 0x3C: "F3", 0x3D: "F4",
    0x3E: "F5", 0x3F: "F6", 0x40: "F7", 0x41: "F8", 0x42: "F9",
    0x43: "F10", 0x44: "F11", 0x45: "F12", 0x35: "GRAVE",
}


def watch(node, seconds):
    """Print raw input reports from the Touch Bar as they arrive.

    Reads the hidraw node directly rather than the evdev node, so what is
    shown is exactly what the T1 sends -- before any keymap is applied. That
    distinguishes "the bar sent nothing" from "the bar sent something the
    input layer discarded".
    """
    import select
    import time

    print(f"\nwatching {node.path} for {seconds}s -- "
          f"TOUCH THE ESC REGION, then a few F-keys.")
    print("(reports are shown as they arrive; nothing here is written)\n")

    fd = os.open(node.path, os.O_RDONLY | os.O_NONBLOCK)
    seen = 0
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [],
                                    max(0, deadline - time.monotonic()))
            if not r:
                continue
            try:
                data = os.read(fd, 64)
            except BlockingIOError:
                continue
            if not data:
                continue
            seen += 1
            keys = []
            # input report 1 is a boot keyboard: id, mods, resv, k1..k6
            if len(data) >= 9 and data[0] == 1:
                for kc in data[3:9]:
                    if kc:
                        keys.append(KEYNAMES.get(kc, f"0x{kc:02X}"))
                if data[1]:
                    keys.append(f"mods=0x{data[1]:02X}")
            label = ("  " + " ".join(keys)) if keys else "  (release)"
            print(f"  {data.hex(' ')}{label}")
    finally:
        os.close(fd)

    print(f"\n{seen} report(s) received.")
    if seen == 0:
        print("Nothing at all arrived. The bar is drawing, but the touch")
        print("regions are not reporting in this mode -- try --mode esc.")


def send_mode(node, mode):
    """Send the layout mode as an output report on the 1.2 interface."""
    rid = None
    for (kind, r), fl in node.reports.items():
        if kind == "Output" and any(f.usage == USAGE_MODE for f in fl):
            rid = r
            break
    if rid is None:
        raise SystemExit("mode field is not in an output report; "
                         "re-run with --dump")

    val = MODE_VALUES[mode]
    print(f"\nmode   : {node.path}  output report {rid} <- {val} ({mode})")
    fd = os.open(node.path, os.O_RDWR)
    try:
        os.write(fd, bytes([rid, val]))
    except OSError as e:
        raise SystemExit(
            f"mode write failed: {e}\n"
            "This is the class request. apple-touchbar uses a VENDOR-type\n"
            "control transfer on T1 instead (apple-touchbar.c:250), which is\n"
            "probably why. The fallback needs usbfs, not hidraw.")
    finally:
        os.close(fd)
    print("mode write ok")


def main():
    ap = argparse.ArgumentParser(
        description="Probe/drive the T1 Touch Bar display over hidraw.")
    ap.add_argument("--set", choices=sorted(DISP_VALUES),
                    help="write the display state (default is read-only)")
    ap.add_argument("--mode", choices=sorted(MODE_VALUES),
                    help="also send the layout mode on interface 1.2 first")
    ap.add_argument("--dump", action="store_true",
                    help="print every decoded feature report, not just 0xFF12")
    ap.add_argument("--watch", type=int, metavar="SECONDS", nargs="?",
                    const=20,
                    help="after any --mode/--set, print raw input reports "
                         "from the bar for SECONDS (default 20)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("run me with sudo (hidraw nodes are root-only)")

    nodes = enumerate_nodes()
    node = find_display_node(nodes)
    path, rdesc, reports = node.path, node.rdesc, node.reports
    print(f"display: {path}  {node.vendor:04x}:{node.product:04x}  "
          f"rdesc={len(rdesc)} bytes")

    mode_node = find_mode_node(nodes)
    if mode_node:
        print(f"mode   : {mode_node.path}  "
              f"{mode_node.vendor:04x}:{mode_node.product:04x}  "
              f"rdesc={len(mode_node.rdesc)} bytes")
    else:
        print("mode   : not found (no 00FF0004 under 00010006)")

    if args.dump:
        print("\n--- all feature reports ---")
        for (kind, rid), fields in sorted(reports.items()):
            if kind != "Feature":
                continue
            print(f"  Feature report {rid} "
                  f"({report_len_bytes(fields)} byte payload)")
            for f in fields:
                print(f"    usage {f.usage:08X} @ bit {f.bit_offset:4d} "
                      f"size {f.bit_size:2d}  (app {f.application:08X})")

    # Locate disp + aux1. They must share a report -- apple-touchbar refuses
    # to act otherwise (apple-touchbar.c:1030).
    target = None
    for (kind, rid), fields in reports.items():
        if kind != "Feature":
            continue
        disp = next((f for f in fields if f.usage == USAGE_DISP), None)
        aux1 = next((f for f in fields if f.usage == USAGE_DISP_AUX1), None)
        if disp and aux1:
            target = (rid, fields, disp, aux1)
            break

    if not target:
        raise SystemExit(
            "found the display usage but not both fields in one feature "
            "report.\nThat is the condition apple-touchbar rejects with "
            "-ENODEV; re-run with --dump and compare against\n"
            "appletb_extract_report_and_iface_info() in apple-touchbar.c.")

    rid, fields, disp, aux1 = target
    payload = report_len_bytes(fields)
    print(f"\nfound  : feature report {rid}, {payload} byte payload")
    print(f"  disp usage {USAGE_DISP:08X} @ bit {disp.bit_offset} "
          f"size {disp.bit_size}")
    print(f"  aux1 usage {USAGE_DISP_AUX1:08X} @ bit {aux1.bit_offset} "
          f"size {aux1.bit_size}")

    if disp.bit_size % 8 or disp.bit_offset % 8 or \
       aux1.bit_size % 8 or aux1.bit_offset % 8:
        raise SystemExit(
            "disp/aux1 are not byte-aligned; this script only handles the "
            "byte-aligned case.\nRe-run with --dump and set the bits by hand.")

    disp_byte = 1 + disp.bit_offset // 8      # +1 for the report-ID prefix
    aux1_byte = 1 + aux1.bit_offset // 8

    # --- GET_FEATURE: read-only, proves the transport --------------------
    fd = os.open(path, os.O_RDWR)
    try:
        buf = array.array("B", [0] * (1 + payload))
        buf[0] = rid
        try:
            fcntl.ioctl(fd, HIDIOCGFEATURE(len(buf)), buf, True)
        except OSError as e:
            raise SystemExit(f"GET_FEATURE failed: {e}\n"
                             "The transport is not working; stop here.")
        current = bytes(buf)
        print(f"\nGET_FEATURE ok: {current.hex(' ')}")
        print(f"  current disp = {current[disp_byte]} "
              f"(1=on 2=dim 4=off), aux1 = {current[aux1_byte]}")

        if not args.set and not args.mode:
            print("\nRead-only run; nothing was written.")
            if args.watch is None:
                print("Re-run with --set on   to light the bar, or")
                print("        with --mode fn --set on   to set the layout.")
                return

        # --- mode first, as apple-touchbar does -------------------------
        if args.mode:
            if not mode_node:
                raise SystemExit("--mode given but no mode field was found")
            send_mode(mode_node, args.mode)

        if args.set:
            set_display(fd, rid, current, disp_byte, aux1_byte, args.set)

        if args.watch is not None:
            if not mode_node:
                raise SystemExit("--watch needs the interface 1.2 node")
            watch(mode_node, args.watch)
        return

    finally:
        os.close(fd)


def set_display(fd, rid, current, disp_byte, aux1_byte, state):
    """Mirror appletb_set_tb_disp(): aux1 forced to 1, disp to the new state."""
    out = array.array("B", current)
    out[0] = rid
    out[aux1_byte] = 1
    out[disp_byte] = DISP_VALUES[state]
    print(f"\nSET_FEATURE  -> {bytes(out).hex(' ')}   (disp={state})")
    try:
        fcntl.ioctl(fd, HIDIOCSFEATURE(len(out)), out, True)
    except OSError as e:
        raise SystemExit(f"SET_FEATURE failed: {e}")
    print("SET_FEATURE ok")


if __name__ == "__main__":
    sys.exit(main())
