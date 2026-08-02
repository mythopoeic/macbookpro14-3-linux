# Linux on a 2016–2019 MacBook Pro: findings that contradict the common advice

Notes from running Ubuntu 26.04 (kernel 7.0) on a **MacBookPro14,3** — the
15-inch 2017 model, Intel HD 630 + Radeon Pro 560, Apple T1/iBridge.

Most of what follows is not in the usual "Linux on a Mac" guides, and several
items directly contradict them. Everything here was measured on hardware. Where
something is untested or uncertain it says so.

This document is deliberately generic: no hostnames, usernames, serial numbers
or network details. Substitute your own where paths appear.

---

## 1. The T1: four dead devices are one fault

On T1 machines (2016–2017 Touch Bar models) the **Touch Bar, Touch ID, ambient
light sensor and FaceTime camera all hang off the T1 coprocessor.** If the T1
has no working firmware, all four are dead simultaneously. People commonly
diagnose these as four separate problems. They are one.

Check the T1's USB ID:

```sh
lsusb | grep 05ac
```

- `05ac:8600` — normal operating mode
- `05ac:1281` — **DFU/Recovery mode**, meaning the firmware is not loaded

### Restoring T1 firmware without touching your Linux install

Installing macOS to an **external** USB drive re-provisions the internal T1
without modifying the internal disk. No source we found documented whether this
would work. It does.

---

## 2. Ubuntu's own udev rule silently disables the T1

**This is a distribution bug and it fails invisibly.** It affects every Ubuntu
user with a T1 Mac.

`/lib/udev/rules.d/39-usbmuxd.rules` (shipped by the `usbmuxd` package) matches
`05ac:8600` and writes `bConfigurationValue=0`, unconfiguring the device so
`usbmuxd` can claim it over libusb.

The failure mode is the worst kind: **`lsusb` still lists the device** while it
has no interfaces at all, and **nothing is logged**. Camera and light sensor
simply do not exist, with no error anywhere.

Fix by shadowing the rule with one in `/etc/udev/rules.d/` of the same name that
drops only the `5ac/8600` pattern. iPhone tethering continues to work.

---

## 3. The Touch Bar does not need a kernel driver

Every guide says you need the out-of-tree `apple-ibridge` + `apple-touchbar`
modules. **You do not.** Two facts settle it:

1. Interface `1-3:1.2` is an **ordinary boot keyboard** already bound by
   `hid-generic` and registered with the kbd handler. Esc and the F-keys have
   always been reaching the input layer.
2. Lighting the panel is **a single HID feature report** on `/dev/hidraw1`,
   which exists alongside whatever driver holds the interface.

The display state reads "on" even when the strip appears dead. What is missing
is the **mode**, which selects the layout the T1 draws. With no mode set it
renders nothing — indistinguishable from a dead backlight.

A userspace daemon of roughly 350 lines provides media keys, Fn to switch to
F1–F12, Esc, and idle dimming, with **no out-of-tree module loaded at all**.

Two kernel panics were spent chasing a capability that already existed in
userspace. If you take one thing from this document, take this one.

### Quirks worth knowing

- The `spcl` strip is ten keys (F1 F2 F5 F6 F7 F8 F9 F10 F11 F12). **F3 and F4
  are unreachable**, not merely unbound.
- `gmux_backlight` is `type=platform`, so `gsd-power` ignores it. A daemon must
  write it directly. Leave volume and playback to the desktop, or you get
  double-stepping.
- Restoring the ambient light sensor enables GNOME auto-brightness, which then
  reverts manual brightness changes between keypresses. The symptom presents as
  "brightness down made it brighter", not "brightness does nothing".

---

## 4. The upstream apple-ibridge analysis is wrong

The usual explanation for the driver's failure is commit `e04955db6a7c` letting
`hid-sensor-hub` claim the interface. That is not the primary cause.

**The real primary bug:** `appleib_ll_parse()` never populates `dev_rdesc`, so
`hid_add_device()` rejects every virtual sub-device with `-ENODEV` — silently.

**The bug that actually panics the machine:** a NULL dereference in
`appleib_forward_int_op()`, which dereferences unfilled `sub_hdevs[]` slots
without a NULL check. Interface `1.2` always leaves the ALS slot NULL. It is
reached via **USB runtime autosuspend**, through `pm_runtime_work`.

A widely-repeated claim that these crashes are `GFP_DMA` exhaustion is wrong.
The boot in question logged zero allocation failures, and the mechanism was
never plausible: in-flight USB buffers are freed on completion, so per-transfer
allocation cannot drain a zone without a leak.

### Read the vmcore, not the journal

`kernel.panic_on_oops=1` plus armed kdump means a crash panics, kexecs, writes a
vmcore, and reboots unattended in about 2.5 minutes. **Panics do not flush the
journal**, so an empty journal is the expected symptom, not evidence of a silent
lockup. The evidence is in `/var/crash/<timestamp>/`.

Two crashes here were misdiagnosed for a day because nobody read the dumps.

### Safety rules if you work on these modules

- **Never `rmmod`** `apple_ibridge` or `apple_touchbar`. After a failed probe,
  kernel memory may already be corrupt; unloading calls `hid_destroy_device()`
  on garbage pointers. Reboot instead — that is the supported way to unload.
- A sysfs unbind is **not** safer. It reaches the same inner path.
- **Reboot between every load attempt.** Never build on a state where these
  modules have loaded once.
- Set `power/control=on` on the T1 and disable lid/idle suspend before any load
  attempt. The autosuspend path is the live crash trigger.

---

## 5. Graphics: the dGPU never downclocks, and you cannot hand off to the iGPU

### amdgpu's `auto` does not downclock on this hardware

Measured over 90 s on a genuinely idle desktop:

| | |
|---|---|
| `sclk` at level 7 of 7 (907 MHz) | **45 of 45 samples — 100 %** |
| mean power | **10.4 W** |
| mean `gpu_busy_percent` | **5.5 %** |

Forcing `power_dpm_force_performance_level=low` pins it to 214 MHz and 7.0 W
with zero variance. **About a third of idle draw, for work it is not doing.**

`power-profiles-daemon` will not do this for you — it reports
`CpuDriver: intel_pstate` and has no GPU driver at all.

### PRIME offload to the iGPU cannot help

```sh
cat /sys/bus/pci/drivers/amdgpu/0000:01:00.0/power/runtime_enabled
forbidden
```

The Radeon **cannot runtime-suspend at all** while it owns the panel and `fb0`.
Rendering an application on the Intel GPU would leave the Radeon fully powered
*and* add a cross-GPU copy per frame — strictly worse.

### The mux route is refused, in every configuration tested

`vga_switcheroo` shows both GPUs registered, so switching looks available. It is
not:

```
$ echo IGD > /sys/kernel/debug/vgaswitcheroo/switch
kernel: vga_switcheroo: client 1 refused switch
```

Tested twice under opposite conditions — with a full desktop session running,
and 11 seconds into boot from a oneshot unit ordered before the display manager,
with i915 essentially idle. **Refused identically both times**, so session state
is not the cause.

A second, independent blocker sits behind it: **i915 registers no eDP connector
at all.** It is destroyed at boot when the link-info read fails, and eDP is a
fixed connector probed once at init, so a hotplug cannot recreate it.

Note also that `fb0` ownership and the runtime-PM decision are both made at
probe time, so no runtime switch could have unlocked runtime PM regardless.

The only mechanism that changes panel ownership before any driver probes is the
Apple EFI `gpu-power-prefs` NVRAM variable, which is persistent and needs an
NVRAM reset to undo. Untested here.

**What macOS does — live mux switching with framebuffer migration — has no Linux
equivalent.** That is a DRM-core and compositor gap, not a driver bug.

### Writing a governor? Watch what it costs

An early version of the idle governor consumed **21.6 % of a CPU core**,
burning more power than it saved and putting the heat on the component that
reaches Tjmax. The cause was forking `readlink` once per process, several
hundred forks per poll.

| implementation | cost per poll | % of a core |
|---|---|---|
| `readlink` fork per process | 1139 ms | 21.6 % |
| `fuser /dev/kfd` for compute detection | 107 ms | ~4 % |
| single `ls`/`grep`, KFD sysfs glob, gated guards | ~14 ms | **0.07 %** |

In a loop that runs every few seconds, **every `$(command substitution)` is a
fork, and forks dominate everything else.** Use `read` and `$EPOCHSECONDS`
builtins rather than `cat` and `date`.

Also: `gpu_busy_percent` spikes to 60–100 % constantly on an idle desktop, while
the 90-second mean is 5.5 %. Any "every sample must be below X" rule therefore
resets forever and never fires. Smooth it.

---

## 6. Thunderbolt and USB-C

### "Dead" USB-C ports may just be a controller that never wakes

Ports recorded as completely non-functional for years — two drives, two cables,
both orientations, zero kernel events — turned out to be **fully working at
10 Gbps**.

The Thunderbolt xHCI serving them has a **broken PCIe PME wake path**. At
`power/control=auto` it sits in `D3hot` and sleeps straight through a device
insertion, logging nothing at all. That is indistinguishable from a dead port.

```
before:  control=auto  power_state=D3hot   bus3=0 bus4=0    <- drive inserted, invisible
echo on > /sys/bus/pci/devices/<xhci>/power/control
t+2s:    state=D0                          bus4=1           <- enumerated, 10 Gbps
```

Two seconds, nothing else changed. A udev rule pinning `power/control=on` fixes
it permanently:

```
ACTION=="add|bind", SUBSYSTEM=="pci", KERNEL=="<pci-id>", ATTR{power/control}="on", ATTR{d3cold_allowed}="0"
```

**The diagnostic that should have prompted a re-test:** the port charged
reliably the whole time. USB-C charging requires PD negotiation over the CC
lines — the same pins that carry attach detection. **A port that negotiates
power is not a dead port.**

### Type-C USB 2.0 goes to the PCH, not through Thunderbolt

Apple routes each Type-C port's **USB 2.0 pair straight to the PCH xHCI** and
only its **SuperSpeed lanes through Thunderbolt**. Consequences:

- A USB 2.0 device in any USB-C port appears on the **PCH bus**, not on the
  Thunderbolt controller's buses.
- Each Thunderbolt xHCI presents a USB 2 companion root hub that **nothing is
  wired to**. Those buses are permanently empty **by design**. An empty
  Thunderbolt USB-2 bus is never evidence of a fault.

### A dock that gives you input devices but no displays has not "negotiated down"

A USB-C dock presents **two independent halves over one cable to two different
host controllers** — USB 2.0 to the PCH, SuperSpeed through Thunderbolt.

"Keyboard and mouse work, monitors don't" means **one of two controllers
failed**. Nothing negotiated down to USB 2. Recovery means resetting the
Thunderbolt controller: driver unbind/rebind, then PCI remove + rescan.

`power/control` and `d3cold_allowed` **must be reapplied after re-probe** — they
do not survive it.

---

## 7. DisplayLink monitor layouts: never key on connector name

Each DisplayLink output is its own `evdi` DRM card, named `DVI-I-1`, `DVI-I-2`,
… **in binding order, which varies between hot-plugs.**

mutter keys `~/.config/monitors.xml` on the full monitor spec — **including the
connector name** — so it accumulates one configuration per permutation and
restores whichever matches. The symptom is a primary display that appears to
move by itself. Nothing is being forgotten; a *different* saved layout is being
restored, correctly, for what mutter believes is a different set of monitors.

**Anything touching monitor layout must identify monitors by EDID (vendor,
product, serial), never by connector.**

Two further traps:

- mutter **will not rebuild its logical monitors** for a config that changes
  only the primary flag. The state serial does not move and `GetCurrentState`
  keeps reporting the old value while `monitors.xml` on disk is already correct.
  Any real geometry change flushes it. Do not trust a primary reading taken
  immediately after setting it.
- Scales must be snapped to the mode's supported list, or mutter rejects the
  entire `ApplyMonitorsChanged` call and the whole layout is lost.

A naive fix that sorts by EDID serial but hardcodes `primary = index 0` will
force the primary onto the built-in panel forever — causing the exact behaviour
it was written to cure.

---

## 8. Audio

### PipeWire without a session manager has *no devices at all*

If every audio device vanishes — not a broken one, none — check the session
manager before suspecting the graphics driver or a codec:

```sh
systemctl --user is-active wireplumber
```

WirePlumber creates the device nodes. PipeWire alone creates none. The ALSA
cards are almost certainly still fine.

**The trap:** the shipped unit is `Restart=on-failure`. A *clean* exit — exit
code 0, which is what a graceful `SIGTERM` produces — is a successful exit, so
systemd correctly declines to restart it. Audio then stays dead indefinitely.

A drop-in fixes it:

```ini
[Service]
Restart=always
RestartSec=2
```

Leave the default start limit in place, so a genuinely broken WirePlumber gives
up rather than spinning.

### Audio already follows your dock — do not write a script for it

WirePlumber stores a **priority-ordered list** of every sink ever explicitly
chosen, in `~/.local/state/wireplumber/default-nodes`:

```
default.configured.audio.sink   = <dock sink>      <- top preference
default.configured.audio.sink.0 = <built-in sink>  <- fallback
```

It selects the highest-priority sink **currently present**, and — critically —
**a fallback does not rewrite the list.** Unplugging the dock drops you to the
built-in speakers without forgetting you preferred the dock, so re-plugging
switches straight back.

The same mechanism applies to sources. Choosing a USB microphone once makes it
return on every re-plug.

A sink-forcing daemon here would race the native mechanism for no benefit.

---

## 9. Thermal

### mbpfan thresholds must sit outside the transient range

`mbpfan` 2.4.0 reads the **instantaneous maximum core temperature** every poll,
with no filtering. These CPUs swing 64 °C → 94 °C **within a single second** on
turbo bursts.

Stock thresholds (`low=63, high=66, max=86` at 1 s polling) all sit inside the
normal working range, so nearly every poll either slammed the fan to maximum or
snapped it back to minimum. Measured, the fan was at full speed **50 % of the
time** and never once at minimum.

Raising `max_temp` close to Tjmax does double duty: sub-second spikes no longer
reach the snap-to-maximum branch, **and** the ramp curve stretches so each step
is roughly 25 rpm instead of ~180. A longer polling interval filters the
sub-second spikes that cause the surging.

**But note the tradeoff runs both ways.** A profile tuned purely for silence
leaves the fans at their floor while the package spikes to 88 °C unanswered, and
the entire fan range unused. Pick thresholds for the goal you actually have.

### RAPL may report no meaningful sustained power cap

On a 45 W-class part, RAPL was found configured as:

```
constraint_0 (long_term)  = 100 W
constraint_1 (short_term) = 125 W
```

Capping PL1 while leaving PL2 alone bounds sustained heat while leaving bursty
interactive work untouched — only long all-core loads are limited.

**Verify the cap while under load, not just after writing it.** `thermald`
actively modulates PL1 under thermal stress (observed moving between 33 W and
42 W against a 40 W setting) and appears to restore the firmware default once
temperatures fall. At idle it does not touch it at all, which makes the revert
easy to miss. Treat your value as a starting point `thermald` then modulates —
it has the final say under load, which is the correct order of authority.

### These chassis run hot by design

A 15-inch MacBook Pro under sustained all-core load sits in the 90s with fans at
maximum. That is not necessarily a fault. Be careful drawing conclusions about
degraded cooling without a known-good baseline for the same model.

---

## 10. GPU compute on Polaris (gfx803)

Upstream ROCm dropped gfx803 after **3.5.1**. That userspace still works against
a modern kernel:

```sh
docker run --rm --device=/dev/kfd --device=/dev/dri --group-add <render-gid> \
  rocm/dev-ubuntu-18.04:3.5.1 /opt/rocm/bin/rocminfo
```

```
ROCk module is loaded
Able to open /dev/kfd read-write
Runtime Version: 1.1
```

`rocm-smi` reads the GPU and `clinfo` enumerates it as an OpenCL device. No
version refusal.

Practical notes:

- **`--group-add <render gid>` is required.** `/dev/kfd` is `root:render`, and
  the host user typically reaches it through a per-session ACL that a container
  does not inherit.
- **`rocminfo` lives in `/opt/rocm/bin`**, which is not on `PATH` in that
  image's default non-login shell. Invoke it by full path.
- **A VM is the wrong tool.** On this hardware the dGPU shares an IOMMU group
  with all three PCIe root ports and the entire Thunderbolt subsystem — 19
  devices. VFIO passes whole groups, so passthrough would take the dock and every
  external display with it. A container needs no passthrough at all.

---

## 11. What is not solvable

- **Touch ID.** Nothing on Linux speaks to Apple's SEP. Do not spend time here.
- **Live GPU mux switching.** Needs framebuffer migration in DRM core plus
  compositor support. Not a driver problem.
- **Some ports are genuinely damaged.** But verify before concluding it —
  see §6.

---

## 12. A note on method

Two of the most useful findings here were corrections of confidently documented
"facts" that nobody had rechecked:

- The upstream crash root cause, which was wrong in this project's own notes and
  on its way to a mailing list, until someone decoded the vmcores.
- Ports documented as completely dead for years, backed by a real test with real
  hardware. The test was sound; the inference from it was wrong. A plausible
  physical explanation (liquid damage elsewhere on the machine) made it
  comfortable to stop asking.

Hardware notes rot. If a claim matters and you have not personally re-verified
it, treat it as a hypothesis. Record what you measured, keep the raw traces —
"before" traces especially cannot be regenerated once you change the config —
and record corrections rather than overwriting them.
