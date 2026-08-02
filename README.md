# Ubuntu on a MacBookPro14,3 (15-inch 2017)

Hardware-enablement notes, configuration and kernel patches for running Linux on
a **MacBookPro14,3** — the 15-inch 2017 MacBook Pro, Intel i7 + Radeon Pro 560,
Apple T1/iBridge.

Tested against **Ubuntu 26.04, kernel 7.0**.

Several conclusions here **contradict the standard advice**, and where that is
the case it is called out with the evidence. Everything was measured on
hardware. Where something is untested or uncertain, it says so.

> **These are one machine's configs.** Paths and hardware IDs are hardcoded on
> purpose — this is single-machine tooling, not a distribution. Read the scripts
> before running any of them, and adapt rather than copy.

---

## Start here

**[`docs/FINDINGS.md`](docs/FINDINGS.md)** is the main document — everything
transferable, organised by topic, with the evidence for each claim.

The three findings most likely to be useful to someone else:

**Ubuntu's own udev rule silently disables the T1.** The packaged
`39-usbmuxd.rules` matches `05ac:8600` and unconfigures the device so `usbmuxd`
can claim it. `lsusb` still lists it while it has no interfaces at all, and
nothing is logged. Camera and ambient light sensor simply do not exist, with no
error anywhere. This affects every Ubuntu user with a T1 Mac.

**The Touch Bar does not need a kernel driver.** Every guide says you need the
out-of-tree `apple-ibridge` + `apple-touchbar` modules. You do not. Interface
`1-3:1.2` is already an ordinary boot keyboard bound by `hid-generic`, and
lighting the panel is a single HID feature report on `/dev/hidraw1`. A userspace
daemon does the lot. Two kernel panics here were spent chasing a capability that
already existed in userspace.

**The upstream `apple-ibridge` analysis is wrong.** The usual explanation is
`hid-sensor-hub` claiming the interface. The real primary bug is
`appleib_ll_parse()` never populating `dev_rdesc`, so `hid_add_device()` rejects
every sub-device with `-ENODEV`, silently. The bug that actually panics the
machine is a NULL dereference reached via USB runtime autosuspend. Four patches
are included.

---

## Contents

| Directory | What's in it |
|---|---|
| [`docs/`](docs/) | The findings document |
| [`t1-ibridge/`](t1-ibridge/) | Apple T1: camera, ambient light sensor, Touch Bar daemon, four kernel patches |
| [`dock-hotplug/`](dock-hotplug/) | USB-C dock recovery, per-dock monitor layout profiles, Thunderbolt xHCI power fixes |
| [`fan/`](fan/) | Fan curve and CPU sustained-power cap, with before/after telemetry |
| [`gpu/`](gpu/) | Discrete GPU idle governor, with telemetry |

Each subsystem README opens with its own root-cause analysis.

---

## Status

| Component | State |
|---|---|
| FaceTime camera | Working |
| Ambient light sensor | Working |
| Touch Bar | Working — userspace daemon, no kernel module |
| USB-C ports (all four) | Working; one pair needed its xHCI pinned out of runtime suspend |
| Audio | Working; survives dock cycles and a killed session manager |
| DisplayLink dock + external monitors | Working; layout stored per dock and monitor combination |
| Fan behaviour | Tuned — see `fan/` for the tradeoff between silence and temperature |
| Discrete GPU idle power | Governed; about a third of idle draw recovered |
| GPU compute (ROCm) | Working on gfx803 via container |
| **Touch ID** | **Not achievable on Linux.** Nothing speaks to Apple's SEP. |
| **Live GPU mux switching** | **Not achievable.** Refused in every configuration tested. |

---

## The kernel patches

Four fixes for `apple-ibridge`, in [`t1-ibridge/patches/`](t1-ibridge/patches/),
applying to upstream commit `4afd309`. `UPSTREAM.md` alongside them carries the
full technical write-up and the evidence, including decoded vmcore analysis.

**Patches `0003` and `0004` have never been loaded on hardware.** Every file
that mentions them says so. Treat them as untested.

They are exported with a placeholder author. If you intend to submit them
anywhere, set a real name and add a `Signed-off-by:` under the DCO first.

**These modules are not needed to make the machine work** — the Touch Bar runs in
userspace. The patches exist because the NULL dereference crashes any T1 machine
running that driver, and upstream should have the fix.

---

## Safety, if you work on the T1 modules

Learned by crashing the machine twice:

- **Never `rmmod`** `apple_ibridge` or `apple_touchbar`. After a failed probe
  kernel memory may already be corrupt; unloading calls `hid_destroy_device()`
  on garbage pointers. Reboot instead — that is the supported way to unload. A
  sysfs unbind is not safer; it reaches the same inner path.
- **Reboot between every load attempt.** Never build on a state where these
  modules have loaded once.
- **Set `power/control=on` on the T1** and disable lid/idle suspend before any
  load attempt. USB autosuspend is the live crash trigger.
- **Read `/var/crash`, not the journal.** Panics do not flush the journal, so an
  empty journal is the expected symptom, not evidence of a silent lockup.

---

## Verification

There is no test suite. Changes are checked syntactically, then observed on the
machine:

```sh
bash -n <script>
udevadm verify /etc/udev/rules.d/<file>.rules
systemd-analyze verify /etc/systemd/system/<unit>
```

Before/after telemetry lives in `fan/tuning/` and `gpu/tuning/`. The "before"
traces are kept deliberately — they cannot be regenerated without reverting the
configuration.

---

## A note on the method

Two of the most useful findings here were corrections of confidently documented
claims that nobody had rechecked: the upstream crash root cause, which was wrong
in this project's own notes and heading toward a mailing list until the vmcores
were decoded; and USB-C ports recorded as completely dead for years on the
strength of a sound test and a wrong inference.

Hardware notes rot. If a claim matters and you have not personally re-verified
it, treat it as a hypothesis.
