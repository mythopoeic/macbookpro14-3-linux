# DisplayLink dock hot-plug recovery

The WAVLINK WL-UG69DK1 dock worked when the machine booted with it attached, but
plugging it in after boot often left the external displays dead — while the
keyboard and mouse on the same cable kept working. The usual reading of that is
"the port negotiated down to USB 2".

## What's actually happening

Nothing negotiates down. The dock presents **two independent halves over one
USB-C cable, routed to two different host controllers**:

| Half | Device | Controller | Carries |
|---|---|---|---|
| USB 2.0 hub | `2109:2813` | PCH xHCI `0000:00:14.0` (bus 1) | keyboard, mouse, mic, webcam |
| USB 3.0 hub | `2109:0813` | Alpine Ridge TB3 xHCI `0000:7d:00.0` (bus 6) | DisplayLink `17e9:6000` |

Apple routes the Type-C port's USB 2.0 pair straight to the PCH, and its
SuperSpeed lanes through Thunderbolt. So the PCH half can enumerate perfectly
while the Alpine Ridge half never appears: input devices work, displays don't.
One of two controllers simply failed — it isn't a speed fallback.

Bus 5 (the Thunderbolt controller's USB 2 companion) has never had a device
attached, which confirms the dock's USB 2 traffic never goes through Thunderbolt.

## The recovery

`dock-xhci-recover` escalates in two stages, triggered by a udev rule on the USB
2.0 half — the one that reliably appears — then checks whether the SuperSpeed
half showed up alongside it.

1. **Stage 1**: unbind and rebind `xhci_hcd` on the Thunderbolt controller.
2. **Stage 2**: PCI `remove` + `rescan` if stage 1 didn't bring it back.

Power settings (`power/control=on`, `d3cold_allowed=0`) are reapplied after
re-probe, since they don't survive it.

Guards: `flock` serialises runs, a 90-second backoff prevents thrashing, and it
exits immediately if the dock is already present. That last one matters — a
transient `error -71` self-healed in 9 seconds during testing and the service
correctly declined to act.

## Monitor layout, and why GNOME kept losing it

Getting the displays *back* is only half of it. They also came back arranged
wrongly, and the primary display moved to the laptop panel on its own.

### The connector names are not stable, and mutter keys on them

Each DisplayLink output is a separate DRM card created by `evdi`, named
`DVI-I-1`, `DVI-I-2`, … in the order the outputs bind. That order varies
between hot-plugs. mutter saves a layout in `~/.config/monitors.xml` keyed on
the full monitor spec — and the spec includes the connector name.

The proof was sitting in `monitors.xml`: **two configurations for the same
three physical monitors**, differing only in which panel got which connector.

| Config | `DVI-I-1` | `DVI-I-2` | Primary |
|---|---|---|---|
| 1 | SERIAL-B | SERIAL-A | **eDP-1 (built-in)** |
| 2 | SERIAL-A | SERIAL-B | DVI-I-1 = SERIAL-A |

The primary had been set during a session where the connectors came up as
config 2. Any later boot that enumerated the other way matched config 1
instead, whose primary is the built-in panel. Nothing was forgotten — mutter
restored a *different* saved layout, correctly, for what it believed was a
different set of monitors.

### The previous fix made the primary problem worse

`~/.local/bin/fix-monitor-order.py` sorted monitors by EDID serial, which was
the right idea, but it also hardcoded `primary = index 0`, and index 0 was the
built-in display. Running under `monitor-order.service` on every
`MonitorsChanged`, it forced the primary back onto the laptop screen every
time. Between the two mechanisms, `gdctl show` was reporting **two** logical
monitors as `Primary: yes` at once.

### One more trap: mutter's live primary flag can go stale

Applying a config that differs *only* in the primary flag does not bump
mutter's state serial and does not rebuild its logical monitors, so
`GetCurrentState` kept reporting the old flag while `monitors.xml` on disk was
already correct. Any change to actual geometry flushes it. `carry_primary()`
in the daemon exists so that a stale reading can never be written back over a
good profile.

### What runs now

[`dock-monitor-profiles.py`](dock-monitor-profiles.py) identifies monitors by
EDID (vendor, product, serial) and **never** by connector, and stores one
profile per dock-and-monitor combination — keyed on the attached monitors plus
the serial numbers of any attached DisplayLink docks, so a second dock gets its
own profiles:

```
$ dock-monitor-profiles show
profile key:  b1e06530be00
docks:        17e9:6000:DOCKSERIAL
```

On a monitor change it either restores that combination's profile, or — if the
set of monitors is unchanged, meaning you just rearranged something — updates
the profile to match. So the stored layout is always the last one that
combination had, which includes position, scale, transform, mode, mirroring and
which monitor is primary.

| Command | |
|---|---|
| `dock-monitor-profiles show` | current layout, profile key, whether it differs from the stored profile |
| `dock-monitor-profiles list` | every stored profile; `*` marks the one attached now |
| `dock-monitor-profiles save` | record the current layout; `--primary <serial>` states the primary explicitly |
| `dock-monitor-profiles apply` | restore the stored profile now |
| `dock-monitor-profiles forget` | delete a profile |
| `dock-monitor-profiles daemon` | what the user unit runs |

`--dry-run` works on `save`, `apply` and `daemon`. Profiles are plain JSON in
`~/.config/dock-monitor-profiles/`, one file each, safe to edit or delete by
hand.

Applies are persistent by default, so mutter records them too and a lucky
connector assignment restores correctly even before the daemon runs;
`--temporary` opts out.

Because the profile key is the set of monitors, unplugging one produces a
different combination and therefore a different profile. That is intended, but
it does mean a transient half-connected state during unplug can create a junk
profile — the 3.5 s settle window absorbs most of them, and `list`/`forget`
clean up the rest.

## The left-hand USB-C ports were never broken

Not dock-related, but it is the same controller family and the same lesson, so it
lives here.

This repo recorded for a long time that the **left-hand USB-C ports do not work
for USB at all** — two drives, two cables, both orientations, zero kernel events,
no device ever appearing on bus 3 or 4. That was wrong. The ports work. A PNY
drive enumerates on one at full **10 Gbps SuperSpeed Plus**.

### Root cause

The left ports are served by `0000:07:00.0`, the Alpine Ridge USB controller
(the right-hand pair is the `0000:7d:00.0` mirror). Its **PCIe PME wake path does
not work**. With `power/control=auto` it sits in `D3hot` and sleeps straight
through a device insertion, logging absolutely nothing:

```
control=auto  power_state=D3hot   bus3=0 bus4=0     <- drive plugged in, invisible
```

Forcing `D0` makes it poll its own ports instead of waiting to be woken. With the
drive still sitting in the port, untouched:

```
$ echo on > /sys/bus/pci/devices/0000:07:00.0/power/control
  t+2s  state=D0  bus4=1
  4-2: USB 3.2.2 FD  speed=10000
```

Two seconds, nothing else changed. The silicon and the connectors were always
fine — the controller simply never woke up to notice.

[`81-left-thunderbolt-xhci.rules`](81-left-thunderbolt-xhci.rules) pins
`power/control=on` at every boot.

### Why this went unnoticed for so long

Both READMEs asserted the ports were dead, citing a real test with real devices.
That test was sound; the conclusion drawn from it was not. A device inserted into
a controller that never wakes is indistinguishable from a dead port, and nothing
is logged either way.

A water spill on this machine — which did damage the right-hand ports, replaced
in early 2022 — made "the left pair is unrepaired damage" a comfortable
explanation. It was written into this repo and was **wrong**. The spill is real;
its connection to the left ports was invented to fit.

The evidence that should have prompted a second look was available all along:
**the left port charges reliably**, and USB-C charging requires PD negotiation
over the CC lines, which are the same pins that carry attach detection. A port
that negotiates power is not a dead port.

### Both ports, both speeds — and why bus 3 stays empty

Verified 2026-08-01:

| | |
|---|---|
| left port 1 | `usb 4-1` — PNY drive, SuperSpeed Plus Gen 2x1, **10 Gbps** |
| left port 2 | `usb 4-2` — same drive, same speed |
| USB 2.0 device | Xbox controller, 12 Mbps, enumerated as **`usb 1-5`** |

That last row looks wrong until you remember the routing this README already
describes: **Apple sends the Type-C port's USB 2.0 pair straight to the PCH and
only its SuperSpeed lanes through Thunderbolt.** So a USB 2 device in a left port
appears on bus 1, not bus 3.

Bus 3 has therefore never seen a device and never will — it is the left
controller's USB 2 companion root hub, and nothing is wired to it. That is the
same architectural fact already recorded for **bus 5** on the right-hand
controller. Both are empty for the same reason, and neither is a fault:

```
bus 3  Thunderbolt USB2 companion (left)    0 events   <- unused by design
bus 5  Thunderbolt USB2 companion (right)   0 events   <- unused by design
bus 4  Thunderbolt SuperSpeed (left)       21 events
bus 6  Thunderbolt SuperSpeed (right)      20 events
bus 1  PCH                                 55 events   <- all USB 2.0, both sides
```

An earlier reading of this treated bus 3 as "the USB 2.0 path for the left ports,
still unproven". It is not a path at all. The knowledge was already here for the
right-hand side and simply was not applied to the left — the same oversight that
left `power/control` unpinned on that controller for years.

### What generalises

This repo already knew that `power/control` on a Thunderbolt xHCI is
load-bearing: `dock-xhci-recover` sets it on `0000:7d:00.0` and `CLAUDE.md`
records that it does not survive re-probe. **Both** Alpine Ridge controllers on
this machine need pinning, for two unrelated reasons — the right one to survive
recovery, the left one because its wake path is broken.

Cost: neither controller runtime-suspends now, so there is a small constant power
draw. Unmeasured, and a fraction of a watt against two usable ports.

## Audio: a re-plug, then no sound devices at all

On 2026-08-01 the machine lost **every** audio device — not a broken one, none
at all — and Steam games began crashing seconds after launch. The obvious
readings were the graphics driver or the Touch Bar work. Both are wrong.

The kernel audio stack never faltered. All five ALSA cards stayed enumerated
throughout, `amdgpu` had no resets or ring timeouts, there were no OOM kills,
and no out-of-tree module was loaded:

```
$ cat /proc/asound/cards
 0 [Webcam ]  1 [C01U ]  2 [Dock ]  3 [PCH ]  4 [HDMI ]
```

What died was **WirePlumber**, PipeWire's session manager. PipeWire itself kept
running the whole time, and PipeWire without a session manager creates no
device nodes whatsoever — which is exactly why the symptom was *zero* devices
rather than a malfunctioning one. `gsd-media-keys` said so at the moment it
happened:

```
Aug 01 16:37:21 wireplumber[4136]: pw.core: leaked proxy 0x... id:3
Aug 01 16:37:21 gsd-media-keys[4873]: Unable to get default sink
Aug 01 16:37:21 gsd-media-keys[4873]: Unable to get default source
```

### Why it never came back

WirePlumber exited *cleanly*, and the shipped unit only restarts on failure:

```
Main PID: 4136 (code=exited, status=0/SUCCESS)
Duration: 16h 28min 35.614s
Result=success   NRestarts=0   ExecMainStatus=0
```

```
$ systemctl --user cat wireplumber.service | grep Restart
Restart=on-failure
```

`Restart=on-failure` is doing precisely what it says: exit 0 is a successful
exit, so systemd declined to restart it. Audio stayed dead for 2h 38m and would
not have recovered without a manual start or a reboot.

### The order of events matters

The dock was re-plugged at 16:29:40, and WirePlumber logged the dock's USB
audio card going away underneath it:

```
Aug 01 16:29:56 wireplumber[4136]: spa.alsa: ALSA close failed: No such device
Aug 01 16:29:56 wireplumber[4136]: spa.alsa: error pcm info: No such device
```

| Time | Event |
|---|---|
| 16:29:40 | dock re-plugged; USB audio card re-enumerates |
| 16:31:25 | Vampire Survivors launched — hangs, never reports "game stopped" |
| 16:36:46 | GNOME **Resources** task manager opened |
| **16:37:21** | **WirePlumber exits 0 — audio devices vanish** |
| 16:37:36 | `steam[16887]: Terminated` |
| 16:39:11, 16:43:04 | games relaunch, die after 36 s and 28 s |

So the causality runs opposite to how it looked. The *first* hang preceded the
audio loss and was its own problem — most plausibly the re-plug pulling the
card mid-session. Every crash *after* 16:37 was caused by the missing audio
device, not the other way round.

What actually terminated WirePlumber is **inferred, not logged**: it exited 35 s
after the task manager was opened, with Steam killed 15 s later, and a clean
exit-0 is the graceful `SIGTERM` path. That is consistent with both being ended
from Resources while chasing the hung game, but nothing records the sender.

### The fix

[`wireplumber-restart.conf`](wireplumber-restart.conf) → a drop-in at
`~/.config/systemd/user/wireplumber.service.d/override.conf`:

```ini
[Service]
Restart=always
RestartSec=2
```

`Restart=always` covers the exit-0 case `on-failure` ignores. Units stopped by
systemd itself — session teardown, or `BindsTo=pipewire.service` stopping — are
still not restarted, so this does not fight logout.

The default start limit (5 starts / 10 s) is **kept deliberately**: a genuinely
broken WirePlumber should give up rather than spin. `RestartSec` is raised from
the stock 100 ms so a real crash loop reaches that limit as a limit.

Recovery is manual if it ever does give up:

```sh
systemctl --user start wireplumber
wpctl status                       # devices, sinks, sources
```

## Which speakers, and why there is no script for it

The monitor layout needed a whole daemon. The audio output needs **nothing** —
and adding a script here would actively break it. The two look like the same
problem and are not, for one specific reason.

WirePlumber does not store "the default sink". It stores a **priority-ordered
list** of every sink you have ever explicitly chosen, in
`~/.local/state/wireplumber/default-nodes`:

```
default.configured.audio.sink=alsa_output.pci-0000_00_1f.3.analog-stereo
default.configured.audio.sink.0=alsa_output.usb-DisplayLink_..._DOCKSERIAL-02.analog-stereo
```

The unsuffixed key is the top preference; `.0`, `.1`, … are fallbacks.
WirePlumber selects the highest-priority sink that is *currently present*, and —
this is the part that makes it work — **a fallback does not rewrite the list**.
Unplugging the dock drops you to the built-in speakers without forgetting that
you preferred the dock, so re-plugging switches straight back.

So "audio follows the dock" is not a feature to build. It is what you get by
picking the dock's speakers once, in Settings → Sound, while docked. Picking
the built-in speakers instead promotes those to the top and audio then stays
on the laptop through any number of dock cycles. One list, whichever you chose
last.

### Why the connector trap does not apply here

`monitors.xml` had to be worked around because mutter keys on the DRM connector
name, and `evdi` hands out `DVI-I-1`/`DVI-I-2` in binding order. The audio node
name has no such instability — it embeds the dock's **serial number**:

```
alsa_output.usb-DisplayLink_USB_Ultra_HD_Graphic_Dock_DOCKSERIAL-02.analog-stereo
                                                      ^^^^^^^^^^^^^
```

That is the same serial `dock-monitor-profiles` keys its profiles on
(`17e9:6000:DOCKSERIAL`). WirePlumber's state holds exactly **one** dock node
name across every re-plug so far, where `monitors.xml` held two configurations
for the same three monitors. Identity by serial, not by enumeration order — so
there is nothing to correct.

### Do not add a sink-forcing script

A daemon that set the default sink on dock hotplug would be
`fix-monitor-order.py` all over again: that script also had the right idea and
still had to be removed, because it fought the native mechanism and left `gdctl`
reporting two primaries at once. WirePlumber is already authoritative here and
already correct. A second writer could only race it.

If a real gap ever appears it would be **per-dock-combination** sinks — the
priority list is global, so a second dock cannot have its own preferred output
the way it gets its own layout profile. That does not affect a one-dock setup
and is not worth building until it does.

```sh
wpctl status                                        # '*' marks the default sink
cat ~/.local/state/wireplumber/default-nodes        # the preference list
wpctl set-default <id>                              # same as picking in Settings
```

## Files

| File | Installed to |
|---|---|
| `dock-xhci-recover` | `/usr/local/sbin/` |
| `dock-superspeed-check.service` | `/etc/systemd/system/` |
| `80-dock-superspeed.rules` | `/etc/udev/rules.d/` |
| `recover-displays` | `~/.local/bin/` (manual trigger) |
| `dock-monitor-profiles.py` | `~/.local/bin/dock-monitor-profiles` |
| `dock-monitor-profiles.service` | `~/.config/systemd/user/` — a **user** unit; it needs the session bus |
| `wireplumber-restart.conf` | `~/.config/systemd/user/wireplumber.service.d/override.conf` |

Install with `install.sh`. It also disables the superseded
`monitor-order.service`, which fights the new daemon directly.

## Status

**SuperSpeed recovery.** Verified: cold boot without the dock, then hot-plug,
enumerates natively at full SuperSpeed — the service correctly did nothing. The
escalation path has **not** yet been exercised against a genuine controller
wedge, because the failure has not recurred since. If stage 2 turns out to
always be required, drop stage 1 and save ~25 seconds.

Check with `journalctl -u dock-superspeed-check`.

**Layout profiles.** Verified on 2026-08-01, with the dock attached:

- saving a profile, and `apply` restoring it exactly
- `apply` clearing the two-primaries state, leaving one primary on the intended
  external monitor
- rearranging the displays and confirming the daemon rewrote the profile
  unprompted, primary included, then restoring the original with no spurious
  re-save

**Not yet exercised:** an actual dock unplug/replug cycle, a reboot, and a
second dock. The unplug path is the one that matters most — it is what the
whole design is for — and it has only been tested by rearranging monitors, not
by changing which monitors are attached.

Check with `journalctl --user -u dock-monitor-profiles`.

**WirePlumber restart drop-in.** Verified on 2026-08-01 by reproducing the
failure exactly — `SIGTERM` to the running process, which is the same graceful
exit-0 path systemd previously refused to restart from:

```
before: pid=41324
  t+1s: active=activating pid=0
  t+2s: active=active pid=42434
Result=success   NRestarts=1   ExecMainStatus=0
```

All five devices returned and `pw-play` produced audible output through both
the dock sink and the built-in speakers afterwards.

**Not yet exercised:** a full logout/login cycle (the claim that `BindsTo=` and
session teardown are unaffected rests on documented systemd behaviour, not on a
test here), the start-limit path under a genuine crash loop, and a real dock
unplug/replug with audio active.

Check with `systemctl --user show wireplumber.service -p Restart -p NRestarts`.

**Speaker selection.** Verified on 2026-08-01 by toggling the dock's ALSA card
profile to `off` and back, which destroys and recreates its sink — the same
event the default-node logic sees on a physical unplug:

| Preference list top | dock off | dock back |
|---|---|---|
| dock | falls back to built-in | **returns to dock** |
| built-in | stays built-in | stays built-in |

The list was not rewritten by either fallback. Default is currently the
built-in speakers, confirmed audible with `pw-play`.

**Not yet exercised:** a physical unplug/replug. The profile toggle exercises
the sink appearing and disappearing, but device removal may not take an
identical code path, and it does not test whether the node name stays stable
across a *cold* re-enumeration — only that it has been stable across the
re-plugs recorded so far.

## Unrelated warning

A boot on 2026-07-31 oopsed inside the Thunderbolt driver itself —
`tb_probe` → `bus_register_notifier` → NULL callback in the notifier chain,
preceded by a double-register warning at `kernel/notifier.c:23`, on NHI
`0000:06:00.0`. No out-of-tree modules were loaded. Seen once. If it recurs it's
a thunderbolt driver bug, not anything here.
