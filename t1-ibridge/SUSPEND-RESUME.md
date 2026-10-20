# Apple T1 Touch Bar on Linux — Suspend/Resume-Safe Setup

Complete setup for the Touch Bar on a MacBook Pro with T1 coprocessor
(MacBookPro14,x, 2016–2017), based on the userspace driver stack from
[mythopoeic/macbookpro14-3-linux](https://github.com/mythopoeic/macbookpro14-3-linux)
(`t1-ibridge`), extended with the pieces required to make the Touch Bar
survive S3 suspend/resume.

**Validated on:** MacBookPro14,2 (2017 13"), elementary OS (Ubuntu-based),
kernel 7.0.0-38-generic. The T1/USB topology is identical on the 14,3.

## Result

| Component | State after this guide |
|---|---|
| Touch Bar after boot | Working (spcl layout, Fn switching, idle dim) |
| Touch Bar after S3 suspend/resume | **Working** (via ACPI `ASOC.SOCW` wake call) |
| FaceTime camera / ambient light sensor | Working |
| Keyboard / trackpad / Wi-Fi after resume | Working |
| Touch ID | Not achievable on Linux (SEP is Apple-proprietary) |

## How it works

The T1 is an ARM coprocessor that runs its own firmware and stays powered
across S3 (it must handle the power button). After resume, its HID control
paths (`disp`/`mode` reports) accept USB transfers but silently discard them
— the rendering subsystem is still asleep, and no amount of HID writes,
USB re-enumeration, or daemon restarts wakes it. The fix is the ACPI wake
method `ASOC.SOCW` on the XHC1 root hub, called through `acpi_call` after
every resume. Disabling USB runtime suspend for the iBridge prevents the
T1 from falling into intermediate sleep states between writes.

---

## Prerequisites

1. **A healthy T1.** Verify the iBridge enumerates as `05ac:8600`
   (recovery mode would show `05ac:1281`):

   ```bash
   lsusb | grep 05ac
   ```

2. **Working S3 suspend/resume.** On my 14,2 this required disabling the
   `XHC2`/`XHC3` ACPI wakeup sources plus a `brcmfmac` suspend hook — that
   setup is outside the scope of this document, but it is a hard
   prerequisite: the steps below assume suspend to RAM works and the
   machine wakes reliably.

3. **Build tooling** for the DKMS module:

   ```bash
   sudo apt install dkms build-essential linux-headers-$(uname -r)
   ```

4. **The repo:**

   ```bash
   git clone https://github.com/mythopoeic/macbookpro14-3-linux.git
   cd macbookpro14-3-linux/t1-ibridge
   ```

---

## Step 1 — Stop usbmuxd from confiscating the T1

Ubuntu's `/lib/udev/rules.d/39-usbmuxd.rules` matches the iBridge
(`5ac/8600`) alongside iOS devices and unconfigures it
(`bConfigurationValue=0`), which silently kills the kernel drivers for the
FaceTime camera and the Touch Bar HID interfaces. The repo ships an override
with the `8600` pattern removed (iPhone/iPad tethering keeps working):

```bash
sudo ./scripts/fix-ibridge-usbmuxd.sh
```

Verify afterwards that the device stays configured and `/dev/hidraw*` nodes
exist. The script's output confirms this live.

## Step 2 — Install the userspace Touch Bar daemon

No kernel modules involved: the T1's interface `1.2` is already delivered as
a boot keyboard by `hid-generic`, and lighting the panel is a single HID
feature report. The daemon (`t1-touchbar-daemon.py`) handles layout switching,
idle dimming, backlight keys and mode re-sends.

```bash
sudo ./systemd/install.sh
sudo systemctl enable t1-touchbar.service
```

This installs:

- `/usr/local/sbin/t1-touchbar-daemon`
- `/etc/systemd/system/t1-touchbar.service`
- `/etc/udev/rules.d/99-t1-touchbar.rules` (starts the daemon when the T1
  enumerates as `05ac:8600`)

Reboot or trigger the udev rule once, then confirm the Touch Bar works and
the daemon maps all keys:

```bash
journalctl -b -u t1-touchbar.service | grep "retargeting"
# expected: "retargeting 12 F-keys to media keys while showing spcl"
```

## Step 3 — Install `acpi_call` (DKMS)

The `acpi_call` kernel module provides `/proc/acpi/call`, which allows
invoking arbitrary ACPI methods from userspace. It is required for the
T1 wake call in Step 5.

**Option A — distro package (preferred, if available):**

```bash
sudo apt install acpi-call-dkms
```

**Option B — build from source via DKMS:**

```bash
git clone https://github.com/mkottman/acpi_call.git
cd acpi_call
sudo make install        # installs module for the running kernel
sudo depmod -A
```

**Load and test:**

```bash
sudo modprobe acpi_call
ls /proc/acpi/call       # must exist
```

The module usually loads on demand; to be safe it is also loaded by the
resume unit itself (`ExecStartPre=/sbin/modprobe acpi_call`), so no boot-time
configuration is strictly required.

## Step 4 — Keep the iBridge permanently powered (udev)

USB runtime autosuspend must stay off for the T1: if the device suspends
between writes, the display state machine gets lost the same way it does
after S3. Extend `/etc/udev/rules.d/99-t1-touchbar.rules` (the file installed
in Step 2) to:

```bash
# Start the userspace Touch Bar daemon when the T1 enumerates healthy.
ACTION=="add|change", SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", ATTR{idProduct}=="8600", \
    TAG+="systemd", ENV{SYSTEMD_WANTS}+="t1-touchbar.service"

# Keep the iBridge permanently powered: no USB runtime suspend.
SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", ATTR{idProduct}=="8600", \
    ATTR{power/control}="on", ATTR{autosuspend_delay_ms}="-1"
```

Notes:

- The `ACTION=="add|change"` extension (upstream repo ships `add` only)
  matters: after S3 the T1 is only *reset*, not re-added, so the daemon
  restart hook on resume relies on the `change` events as well.
- The power lines have **no** `ACTION` match on purpose — the attributes
  shall be re-applied on every event.

Apply and verify:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=usb

cat /sys/bus/usb/devices/1-3/power/control            # must print: on
cat /sys/bus/usb/devices/1-3/power/autosuspend_delay_ms   # must print: -1
```

## Step 5 — The resume hook: wake the T1 via `ASOC.SOCW`

Create `/etc/systemd/system/t1-touchbar-resume.service`:

```ini
[Unit]
Description=Wake Apple T1 (iBridge) and restart touchbar daemon after suspend
After=suspend.target

[Service]
Type=oneshot
ExecStartPre=/sbin/modprobe acpi_call
ExecStart=/bin/sh -c "echo '\_SB.PCI0.XHC1.RHUB.ASOC.SOCW 0x1' > /proc/acpi/call; sleep 1; systemctl restart t1-touchbar.service"

[Install]
WantedBy=suspend.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable t1-touchbar-resume.service
```

What this does:

1. `After=suspend.target` + `WantedBy=suspend.target` makes the unit run in
   the resume phase, after the machine is awake.
2. `\_SB.PCI0.XHC1.RHUB.ASOC.SOCW 0x1` wakes the T1's rendering subsystem
   through the firmware's own init path — the exact call HID writes cannot
   reach.
3. The daemon restart re-sends mode (`spcl`) and display-on on the freshly
   woken T1.

**Path caveat:** the exact ACPI path was verified on a MacBookPro14,2 as
`\_SB.PCI0.XHC1.RHUB.ASOC.SOCW`. If it does not work on your model, dump
your DSDT and locate the method (`SOCW` under the `ASOC` device):

```bash
sudo cat /sys/firmware/acpi/tables/DSDT > dsdt.dat
iasl -d dsdt.dat && grep -n "SOCW" dsdt.dsl
```

## Step 6 — Daemon hardening (recommended)

The daemon can hang on a `SIGTERM` when its hidraw FDs die mid-write, which
stalls the suspend sequence. Add a stop timeout override:

```bash
sudo systemctl edit t1-touchbar.service
```

```ini
[Service]
TimeoutStopSec=5
```

```bash
sudo systemctl daemon-reload
```

---

## Verification

Run **at least three** consecutive suspend/resume cycles — the second cycle
was historically the failure case:

```bash
sudo systemctl suspend
# wake, check the Touch Bar
# ... repeat 2-3 times
```

Then confirm the whole chain:

```bash
# 1. Daemon restarted after resume, all keys mapped:
journalctl -b -u t1-touchbar.service | grep -E "Started|retargeting"
# expected per cycle: "Started t1-touchbar.service" and "12 F-keys"

# 2. Resume unit ran successfully:
journalctl -b -u t1-touchbar-resume.service

# 3. T1 state is sane (disp=1, brightness 0x03e8=1000 when lit):
sudo python3 scripts/touchbar-hidraw-probe.py
# expected when bright:
#   GET_FEATURE ok: 02 05 01 00 00 00 00 e8 03 00 00

# 4. Power management still applied:
cat /sys/bus/usb/devices/1-3/power/control   # on
```

## Troubleshooting: what a dark bar after resume means

The failure signature (before the `ASOC.SOCW` fix) was fully characterized
and worth knowing when debugging:

- The daemon runs cleanly, watches the correct input nodes, and the HID
  writes report success — but the bar stays dark from the first second.
- `touchbar-hidraw-probe.py` shows the T1 state machine stuck (e.g.
  `disp=4`, brightness 0) and **does not change** after `SET_FEATURE` —
  the write is acknowledged at the USB level and discarded internally.
- Output reports (mode switching via Fn) are equally ignored.

Things that do **not** fix it (all verified): daemon restart (immediately or
minutes later), full USB re-enumeration via `authorized` 0/1 toggle, HID
driver unbind/rebind cycles, forced mode cycles (`off` → `spcl`), delayed
daemon starts. Only the ACPI `ASOC.SOCW` wake call restores the panel —
which matches the T1 being a coprocessor that stays powered through S3 and
needs a firmware-level re-init on wake.

## Known limitations

- **Touch ID** does not work on Linux (Apple SEP, no protocol available).
- **F3/F4** are not reachable: the T1 draws no Mission Control/Launchpad
  keys in `spcl` mode.
- The `apple-ibridge`/`apple-touchbar` out-of-tree kernel modules are
  **not** needed and should **not** be loaded — the upstream repo documents
  a NULL dereference in `appleib_hid_suspend()` that crashes the kernel in
  the suspend path on kernel 7.x. The userspace daemon covers the same
  functionality.

## References

- [mythopoeic/macbookpro14-3-linux — t1-ibridge](https://github.com/mythopoeic/macbookpro14-3-linux/tree/master/t1-ibridge)
- [mkottman/acpi_call](https://github.com/mkottman/acpi_call)
