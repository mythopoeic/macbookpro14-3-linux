# Apple T1 / iBridge on Ubuntu

The T1 is a small ARM coprocessor in the 2016–2017 Touch Bar MacBook Pros. The
Touch Bar, Touch ID, the ambient light sensor and the **built-in FaceTime
camera** all hang off it. When the T1 has no OS to run, all four are dead at
once — one fault, not four.

On this machine (MacBookPro14,3) the T1 had been left without firmware and
enumerated as `05ac:1281` (Apple Mobile Device, Recovery Mode) instead of the
healthy `05ac:8600` (iBridge).

## Outcome

| | Status |
|---|---|
| T1 firmware | **Restored** — `05ac:8600` |
| FaceTime camera | **Working** — `/dev/video0`, uvcvideo, verified in a video call |
| Ambient light sensor | **Working** — `iio:device0`, name `als` |
| Touch Bar | **Working** — Esc, F-keys, media keys, Fn switching, idle dimming. No kernel module involved; see section 3 |
| Touch ID | Not achievable on Linux; nothing speaks to Apple's SEP |

## 1. Restoring the T1 firmware

There is no standalone firmware image for the T1. Apple's bridgeOS manifest
lists only `iBridge2,x` (the T2); the T1 is `iBridge1,1` and appears nowhere. So
`idevicerestore` and Apple Configurator are both dead ends — the transport works,
there is simply nothing to feed them. T1 firmware ships **only inside macOS**,
flashed by an EFI updater (`MultiUpdater.efi`) at boot.

**What worked: installing macOS to an external USB drive and running Software
Update until exhausted.** This re-provisions the internal T1 without touching
the internal disk. No source documented whether that would work; it does.

Full procedure: [touchbar-revival-procedure.md](touchbar-revival-procedure.md).

The single command that decides it:

```sh
lsusb | grep 05ac      # 8600 = success, 1281 = still in recovery
```

## 2. Stopping usbmuxd from confiscating the device

Getting `05ac:8600` is not enough on Ubuntu. `/lib/udev/rules.d/39-usbmuxd.rules`
matches the iBridge alongside iOS devices and **deliberately unconfigures it**:

```
ENV{PRODUCT}=="5ac/12[9a][0-9a-f]/*|5ac/8600/*", ACTION=="add", \
    ATTR{bConfigurationValue}="0", OWNER="usbmux", ENV{SYSTEMD_WANTS}="usbmuxd.service"
```

The T1 speaks the same usbmux protocol as an iPhone, so `usbmuxd` claims it over
libusb and evicts the kernel drivers — including `uvcvideo`, which is what
actually drives the camera.

**The symptom is distinctive and easy to misread:** the device configures
normally at boot, HID interfaces bind and log, and then everything silently
disappears. Writing `0` to `bConfigurationValue` produces no kernel message at
all, and `lsusb` still lists the device, so it looks healthy while having no
interfaces.

Fix: [udev/39-usbmuxd.rules](udev/39-usbmuxd.rules) — same filename in
`/etc/udev/rules.d/` takes precedence over `/lib`. Identical to the packaged
rule minus the `5ac/8600` pattern, so iPhone and iPad tethering still work.

Install with [scripts/fix-ibridge-usbmuxd.sh](scripts/fix-ibridge-usbmuxd.sh).
The camera and light sensor come up immediately afterward and survive reboots.

## 3. Touch Bar — it never needed a kernel driver

**The Touch Bar works, and none of the out-of-tree kernel code is involved.**

The whole effort had been aimed at getting `apple-ibridge` and `apple-touchbar`
to bind, because that is what every guide says is required. Probing the hardware
directly showed the premise was wrong. Two facts, both checkable in a minute:

- **Interface `1-3:1.2` is an ordinary boot keyboard**, bound by `hid-generic`,
  registered at `event4` with the `kbd` handler. Touch Bar key presses have
  always reached the input layer — Esc included, HID usage `0x29`. Nothing was
  needed to make the keys work.
- **Lighting the panel is one HID report.** The display state lives in feature
  report 2 on interface `1-3:1.3`, reachable through `/dev/hidraw1`, which
  already exists alongside whatever driver holds the interface.

So `apple-ibridge`'s entire job — synthesising virtual HID sub-devices so
`apple-touchbar` has something to bind to — is machinery in service of a
capability that was already there.

Worth noting what the display field said when first read:

```
GET_FEATURE ok: 02 05 01 00 00 00 00 e8 03 00 00
                   ^^ disp = 1 = ON
```

The panel was already "on" while dark. The missing piece was never the display
state; it was the **mode**, which selects the layout the T1 draws. With no mode
set it renders nothing, which looks exactly like a dead backlight.

### What runs now

[`scripts/t1-touchbar-daemon.py`](scripts/t1-touchbar-daemon.py) — userspace,
`hidraw` + `evdev`, no modules:

- media keys at rest, F1–F12 while **Fn** is held
- dims after 60 s idle, off after 60 s more, restores on any keyboard, trackpad
  or Touch Bar activity
- re-sends the mode on wake, since the T1 can forget the layout while dark
- retargets the F-keys to match the glyphs on screen, and drives screen and
  keyboard backlight itself

The bar reports plain `F1`–`F12` whatever it is drawing, so the host decides
what they mean — the same job `appletb_input_configured()` does. It has to
follow the mode, though: a static `hwdb` rule could not, because holding **Fn**
must still produce real function keys. So the daemon rewrites the live keymap
with `EVIOCSKEYCODE_V2` on each switch, discovering scancodes by walking the
keymap rather than assuming the HID usage encoding, and restoring plain F-keys
on exit so a crash cannot leave them hijacked.

**Measured layout.** The `spcl` strip is **ten** keys, not twelve, sending

```
F1  F2  F5  F6  F7  F8  F9  F10  F11  F12
```

`F3` and `F4` are absent — the T1 does not draw Mission Control or Launchpad at
all. The driver's table marks those two `/* not used */`, which understates it:
they are unreachable, not merely unbound.

Install with [`systemd/install.sh`](systemd/) — daemon to `/usr/local/sbin`, a
unit, and a udev rule that fires on `05ac:8600` only. Report IDs and field
offsets are discovered by parsing the descriptors at runtime, so a firmware
change produces a clean error rather than a wrong write.

```sh
sudo systemd/install.sh && sudo systemctl enable --now t1-touchbar
journalctl -fu t1-touchbar
```

### Restoring the ALS silently broke manual brightness

Worth knowing, because the two look unrelated and the symptom is misleading.

Once the ambient light sensor works, GNOME notices it and turns on automatic
brightness (`org.gnome.settings-daemon.plugins.power ambient-enabled`). It then
drives `gmux_backlight` continuously and reverts every manual change. The
symptom is not "brightness does nothing" — it is **"brightness down made it
brighter"**, because the panel dips for a few milliseconds and springs back to
whatever the sensor asks for, faster than the eye resolves.

The daemon's own log made it obvious once it printed the pre-read value:

```
backlight gmux_backlight: 1023 -1*51 -> wrote 972, reads 972
backlight gmux_backlight: 1023 -1*51 -> wrote 972, reads 972   <- back to 1023 already
```

Our write landed every time and was undone between keypresses. Fix:

```sh
gsettings set org.gnome.settings-daemon.plugins.power ambient-enabled false
```

That is a per-user setting, so it applies to one GNOME session only — not to
root, a greeter, or another account. Reverse it with `true`. It is a real
trade: the panel no longer adapts to room lighting, in exchange for the
brightness keys working at all.

GNOME does **not** drive this backlight itself even with auto-brightness off —
`gmux_backlight` is `type=platform` and `gsd-power` leaves it alone, verified by
running the daemon with `--no-backlight` and watching nothing happen. So the
daemon writes sysfs directly. Volume and playback are deliberately left to the
desktop, which does handle those; doing both would double-step.

### The kernel patches are still worth having

They are no longer a prerequisite for anything here, but three of the bugs are
real and upstream should have them — in particular the NULL dereference, which
crashes any T1 machine running that driver.

## 4. The kernel driver route — four patches, and an unresolved problem

[patches/](patches/) contains four fixes to `t2linux/apple-ib-drv` plus a full
write-up. Short version:

- **`0001`** fixes an out-of-bounds write that corrupts kernel memory.
- **`0002`** fixes `appleib_ll_parse()` never populating `dev_rdesc`, which is
  the actual reason the Touch Bar stopped working on modern kernels — not the
  commit usually blamed for it.
- **`0003`** fixes a NULL dereference on unfilled sub-device slots, reached from
  the USB autosuspend path. This is what panicked the machine. **Untested.**
- **`0004`** hands sub-devices the post-`report_fixup()` descriptor rather than
  the raw one. **Untested.**

`0001` and `0002` are verified on hardware; `0003` and `0004` build cleanly and
apply from upstream `4afd309`, but have not been loaded yet.

With both applied the driver chain builds and binds correctly. The display still
does not light: `apple-touchbar` also needs the display interface, which lives
only on `1.3`, and binding `1.3` runs into a **third, unpatched bug** — a NULL
dereference in `appleib_forward_int_op()` reached from the USB autosuspend path.
Separately, `apple-ibridge` gives each virtual sub-device the whole report
descriptor, so `hid-sensor-hub` claims the virtual Touch Bar device instead of
`apple-touchbar`. Details, vmcore evidence and a two-line candidate fix for the
second problem are in [patches/UPSTREAM.md](patches/UPSTREAM.md); the order of
work is in [NEXT-STEPS.md](NEXT-STEPS.md).

An earlier version of this section said the machine died from `GFP_DMA`
exhaustion. The kdump vmcore disproves that — see the correction at the end of
`UPSTREAM.md`.

### Danger

Loading these modules crashed this machine twice. Rules learned the hard way:

- **Never `rmmod`.** After a failed probe, kernel memory is already corrupt and
  unloading calls `hid_destroy_device()` on garbage. Reboot instead. A sysfs
  unbind is **not** a safe substitute — same inner teardown path.
- **Reboot between attempts**, so you never build on a corrupted state.
- **Disable USB autosuspend and lid suspend first.** That path is the live
  trigger for bug 3, and it fires with no user action.
- **Do not run `scripts/bind-iface3.sh`.** It is kept for the record and is
  disabled at the top of the file. It binds successfully, then the machine
  panics ~11 seconds later on bug 3.

**Recovery is automatic.** `kernel.panic_on_oops=1` with kdump armed: the machine
panics, kexecs, writes a vmcore to `/var/crash`, and reboots on its own in ~2.5
minutes. Read `/var/crash`, not the journal — panics do not flush the journal,
which is why both crashes looked like silent hard locks.

## Scripts

| Script | Purpose | Safe? |
|---|---|---|
| `t1-touchbar-daemon.py` | **The Touch Bar driver.** Userspace, hidraw + evdev | Yes; no modules |
| `touchbar-hidraw-probe.py` | Inspect/drive the display and mode reports by hand | Yes; read-only unless `--set`/`--mode` |
| `fix-ibridge-usbmuxd.sh` | Installs the udev override; restores camera + ALS | Yes |
| `probe-touchbar2.sh` | Unbuffered check for Touch Bar input | Yes, read-only |
| `attempt-touchbar.sh` | Guarded module load; aborts cleanly on trouble | Loads modules |
| `bind-iface3.sh` | Binds interface 1.3 — **panics the machine on bug 3** | **DISABLED** |

`touchbar-attempt.log` is the recorded output, written with `sync` after every
step. It survived, but it is not what made the crashes diagnosable — the kdump
vmcores in `/var/crash` were.

## 5. Surviving suspend/resume

The Touch Bar goes dark after every S3 resume and cannot be revived through
HID writes or USB re-enumeration — the T1 needs a firmware-level wake call.
See [/t1-ibridge/SUSPEND-RESUME.md](SUSPEND-RESUME.md) for the full setup.
