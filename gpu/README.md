# Discrete GPU idle power

The Radeon Pro 560 draws **10.4 W doing nothing**. Not while gaming — while
sitting on an empty desktop. The obvious reading is "the dGPU should hand off to
the Intel iGPU when it isn't needed, the way macOS does". That is not what is
wrong, and it is not what this fixes.

## Root cause

`amdgpu`'s own `auto` power management never downclocks on this machine.
Measured over 90 s on an idle desktop with Steam closed:

| | |
|---|---|
| `sclk` at level **7 of 7** (907 MHz) | **45 of 45 samples — 100 %** |
| mean power | **10.38 W** (min 10.04, max 11.15) |
| mean `gpu_busy_percent` | **5.5 %** |
| samples showing any load at all | 5 of 45 |

The shader clock never left maximum while the GPU reported doing essentially
nothing. Forcing the low performance level pins it to 214 MHz:

| Setting | sclk | Power | Variance |
|---|---|---|---|
| `auto` | 7: 907 MHz | 10.38 W | 10.04 – 11.15 |
| `low` | 0: 214 MHz | **7.01 W** | none — 7.01 across all samples |

**3.37 W, about a third of the GPU's idle draw**, for work it is not doing.
Raw traces in [tuning/](tuning/).

## Why not hand off to the iGPU

Because it cannot help, and the reason is worth writing down.

```
$ cat /sys/bus/pci/drivers/amdgpu/0000:01:00.0/power/runtime_enabled
forbidden
```

The Radeon **cannot runtime-suspend at all**. It owns the internal panel and
`fb0`, so the driver forbids runtime PM outright. Rendering an application on
the Intel GPU via PRIME would leave the Radeon fully powered *and* add a
cross-GPU copy per frame — strictly worse. The iGPU meanwhile sits at
`runtime_status=suspended` driving nothing, with its eDP disabled at boot:

```
i915 0000:00:02.0: [drm] [ENCODER:109:DDI A/PHY A] failed to retrieve link info, disabling eDP
i915 0000:00:02.0: [drm] Cannot find any crtc or sizes
```

Powering the Radeon down at all means moving the panel off it through the mux.
That is a different problem, below.

## What macOS does, and why this isn't that

Apple's Automatic Graphics Switching drives the *same* gmux that `apple_gmux` is
bound to here. It switches the mux **live**, migrating the framebuffer between
GPUs mid-session, and **power-gates** the dGPU entirely.

| | macOS | This governor |
|---|---|---|
| dGPU actually off | yes | no — clamped to 214 MHz |
| Switches live | yes | n/a |
| Automatic by demand | yes | yes |
| Recovered | ~10–14 W | 3.4 W |

Linux has no equivalent of WindowServer's live framebuffer migration. This
recovers a third of the gap with none of the risk.

## The mux route — refused at runtime, tested

The hardware supports it. Both GPUs are registered `vga_switcheroo` clients:

```
$ sudo cat /sys/kernel/debug/vgaswitcheroo/switch
0:DIS:+:Pwr:0000:01:00.0        <- Radeon, '+' = currently drives the display
1:IGD: :Pwr:0000:00:02.0        <- Intel, registered
2:DIS-Audio: :DynOff:0000:01:00.1
```

**A runtime switch is refused. Tested twice, under opposite conditions:**

```
$ echo IGD > /sys/kernel/debug/vgaswitcheroo/switch
kernel: vga_switcheroo: client 1 refused switch
```

| Attempt | Conditions | i915 `refcnt` | Result |
|---|---|---|---|
| 2026-08-01, session up | Xwayland, Chrome, VS Code running | 5 | refused |
| 2026-08-01, boot-time unit | 11 s into boot, before the display manager | **1** | **refused** |

Nothing moved either time — `0:DIS:+` before and after, `fb0` still
`amdgpudrmfb`, `eDP-1` still on `card1`. The screen never went dark because the
switch never happened.

**The first explanation recorded here was wrong.** It attributed the refusal to
i915 having open DRM clients (Xwayland, Chrome, VS Code). The boot-time attempt
refused identically with i915 essentially idle and the graphical session not yet
started, so session state was never the cause.

The likelier reading is that "client 1" is the **discrete** GPU, not the
integrated one: the leading digits in the `switch` file are list indices
(`0:DIS`, `1:IGD`), whereas the refusal message prints the client enum, in which
`DIS = 1`. That fits — amdgpu owns `fb0`, the console and the panel, and its
`can_switch` returns false while its open count is non-zero, which it always is.
The enum mapping is **inferred**; what is directly evidenced is that the refusal
does not depend on i915's state.

A **second** blocker sits behind it, still untested. i915 registers no eDP
connector at all:

```
card0-DP-5  card0-DP-6  card0-HDMI-A-1  card0-HDMI-A-2      <- no eDP
```

It is destroyed at boot when the link-info read fails, and eDP is a fixed
connector probed once at init, so a hotplug cannot recreate it. The boot test was
built to rebind i915 with the mux already moved and see whether eDP then
appears — but it never reached the rebind, because the switch was refused first.
**Whether i915 can drive this panel remains unknown.**

### What that leaves

Runtime switching is refused with the session up *and* at early boot, so
ordering a unit before the display manager does not help, and neither does the
fact that `i915`/`amdgpu` are absent from the initramfs. The only mechanism that
changes which GPU owns the panel before any driver probes is the Apple EFI
`gpu-power-prefs` NVRAM variable — which is persistent and needs an NVRAM reset
to undo. That is the one thing this work has deliberately not touched.

`fb0` ownership and the runtime-PM decision are both made at probe time, so no
runtime switch could ever have unlocked runtime PM regardless of the above.

If it is ever revisited, one fact lowers the risk: both external monitors are
DisplayLink (`evdi`, over USB) and sit on no GPU's display engine, so a failed
attempt costs the internal panel but leaves a working desktop to recover from.

## How the governor decides

Poll `gpu_busy_percent`; clamp when quiet, release when not.

| | |
|---|---|
| clamp when | smoothed busy% (EMA, α=1/8) ≤ `IDLE_EMA` (20) |
| release when | a user hold, **or** `/dev/kfd` open, **or** Steam/a game running, **or** raw busy% ≥ 40 |
| warmup | 10 samples (~30 s) before the first clamp |
| cooldown | 30 s after a release before re-clamping |
| worst-case release latency | **3 s** |

**Release is deliberately far more eager than clamping.** This is a development
box whose dGPU is used for compute, so the two errors are not symmetric: a late
release means a compute kernel running at 214 MHz, a premature release costs a
few watts. The original 60 %-for-2-polls rule gave a 10 s worst case, long
enough for a short kernel to run entirely clamped — and a slow kernel does not
announce itself the way a stuttering game does, it just looks like slow code.
Now any single sample at 40 % releases within one poll.

**The clamp never caps peak performance.** It only applies while genuinely idle,
and releasing restores `auto` — the full 214–907 MHz range. Nothing here limits
the GPU under load; the only cost was ever the transition latency, which is what
the retuning above addresses.

**Thresholds work on a smoothed average, not raw samples, and that is the whole
design.** The first version required every sample below a threshold. It never
clamped once, because `gpu_busy_percent` spikes to 60–100 % constantly on an
idle desktop — evdi copies for the two DisplayLink screens, compositor work,
terminal redraws — while the 90 s mean is 5.5 %. Any "reset on spike" rule
resets forever.

Release is asymmetric with clamping on purpose: being slow to clamp costs watts,
being slow to release costs frames.

### GPU compute actually works — via a container, not a VM

Verified 2026-08-01. The kernel reports this GPU as `gfx_target_version 80003`
(gfx803, Polaris 11), which is exactly what ROCm supported up to **3.5.1** before
dropping it. That 2020-era userspace talks to this 2026 kernel without complaint:

```
$ docker run --rm --device=/dev/kfd --device=/dev/dri --group-add 990 \
    rocm/dev-ubuntu-18.04:3.5.1 /opt/rocm/bin/rocminfo
ROCk module is loaded
Able to open /dev/kfd read-write
Runtime Version: 1.1

$ ... /opt/rocm/bin/rocm-smi
GPU  Temp   SCLK    MCLK    Perf  PwrCap
1    51.0c  320Mhz  300Mhz  auto  35.0W

$ ... /opt/rocm/opencl/bin/clinfo
Platform Name: AMD Accelerated Parallel Processing (OpenCL 2.0 AMD-APP 3137.0)
Number of devices: 1    Device Type: CL_DEVICE_TYPE_GPU    Vendor ID: 1002h
```

No version refusal, no errors. **A container is the right tool here and a VM is
not** — the Radeon sits in IOMMU group 1 alongside all three PCIe root ports and
the entire Thunderbolt subsystem including `0000:7d:00.0`, the dock's SuperSpeed
xHCI. VFIO passes whole groups, so handing the GPU to a VM would take the dock
and both external monitors with it. A container needs no passthrough at all,
only `/dev/kfd` and `/dev/dri`.

`--group-add 990` is required: `/dev/kfd` is `root:render 660`, and the host user
reaches it through a per-session ACL (`user:youruser:rw-`) that a container
does not inherit.

`rocminfo` lives at `/opt/rocm/bin`, which is not on `PATH` in that image's
default non-login shell — invoke it by full path.

### The compute guard

`/dev/kfd` is AMD's compute device. ROCm, HIP, and any OpenCL stack layered on
them open it; nothing else does. Any process holding it open means GPU compute
is in flight, and the governor will not clamp — no heuristics, no process names.
That makes it definitive in a way names never are, which is the same lesson the
game guard learned the hard way below.

Vulkan and rusticl compute go through the render node rather than `/dev/kfd`, so
they are caught by the busy% path instead. On this GPU that matters: it is
Polaris 11 / **gfx803**, which upstream ROCm dropped support for years ago, so a
working compute stack here is most likely rusticl (OpenCL 3.0 on Mesa) or Vulkan
compute rather than ROCm. `/dev/kfd` exists and the kernel side initialises
(`kfd kfd: added device 1002:67ef`), so the guard is live regardless of which
userspace ends up installed.

### What the daemon itself cost — and why that nearly sank it

A power-saving daemon has to be cheaper than what it saves. The first two
versions were not, and systemd said so plainly on every restart:

| version | CPU reported | % of a core |
|---|---|---|
| `readlink` fork per process, poll 5 s | 10 min 9 s over 47 min | **21.6 %** |
| `ls`/`grep` but `fuser /dev/kfd`, poll 3 s | 2 min 31 s over 8 min 15 s | **30.4 %** |
| KFD sysfs glob, guards skipped when busy | 0.04 s over 60 s | **0.07 %** |

At 21.6 % of a core the governor was burning on the order of 1–2 W of **CPU**
power to save 3.4 W of **GPU** power — consuming perhaps half its own benefit,
and moving heat onto the component that reaches Tjmax from the one that idles at
49 °C. In thermal terms that is worse than not running it at all. Shortening the
poll to 3 s to protect compute latency made it *worse* before it got better.

Three measured fixes:

- **`game_running` forked `readlink` once per process**, several hundred forks
  per poll. A single `ls -l /proc/[0-9]*/exe | grep` does the same job:
  1139 ms → 14.3 ms per call, **88×**.
- **`fuser /dev/kfd` cost 107 ms** because it walks every process's file
  descriptors. KFD keeps its own per-client accounting at
  `/sys/class/kfd/kfd/proc/`, populated the moment `/dev/kfd` is opened, so the
  check becomes a fork-free bash glob: 0.045 ms, **2400×**.
- **The guards only run when they can change the outcome** — while clamped, or
  when a clamp is imminent. A busy, unclamped GPU skips both entirely, which is
  the heavy-use case where the waste would matter most.

The general lesson is worth keeping: in a loop that runs every few seconds,
every `$(command substitution)` is a fork, and forks dominate everything else.
The hot path now uses `read` and `$EPOCHSECONDS` builtins rather than `cat` and
`date`.

### The manual hold

For a work session where the clamp should stay out of the way whatever the
heuristics think:

```sh
sudo amdgpu-governor hold      # release now, stop clamping entirely
sudo amdgpu-governor unhold    # resume normal governing
```

The hold is a flag file in `/run`, so it does not survive a reboot — it cannot
be left on by accident.

### The game guard

Matches the **resolved executable** (`/proc/PID/exe`), never the command line.
`pgrep -f` was the first implementation and was wrong — it matches argv, so any
shell, editor or `grep` whose command line merely mentions `steamapps` trips the
guard and silently disables the governor. Testing found the governor
permanently guarded by its own test harness. `/steamapps/` covers both the game
and Proton, since Proton installs under `steamapps/common` too.

## Usage

```sh
amdgpu-governor status                # level, busy%, sclk, watts, guard state
amdgpu-governor --dry-run daemon      # log transitions without writing
amdgpu-governor low | auto            # one-shot manual override
journalctl -u amdgpu-governor -f
```

Every knob is an environment variable (`POLL`, `IDLE_EMA`, `RELEASE_BUSY`,
`WARMUP`, `COOLDOWN`, `GUARD_STEAM`), overridable in the unit.

The device is resolved through the `amdgpu` driver link, never
`/sys/class/drm/cardN` — card numbering is not stable across boots on a machine
with four `evdi` cards, and this repo has been bitten once already by trusting
enumeration order over a stable identifier.

## Files

| File | Installed to |
|---|---|
| `amdgpu-governor` | `/usr/local/sbin/` |
| `amdgpu-governor.service` | `/etc/systemd/system/` |

Install with `install.sh` (needs root). To revert entirely:

```sh
sudo systemctl disable --now amdgpu-governor    # restores 'auto' on stop
```

`ExecStopPost` restores `auto` even if the daemon is killed uncatchably, so the
GPU cannot be left pinned at 214 MHz by a crash.

## Status

**Verified** on 2026-08-01:

- idle baseline, 90 s / 45 samples — the 100 % level-7 figure above
- clamp effect, 7 samples at 7.01 W with zero variance
- both external DisplayLink screens driven at 214 MHz with **no stutter**
  (observed directly during the clamp probe)
- guard blocks clamping: with a real executable under `steamapps/`, the daemon
  logged 0 clamp transitions; with it gone, the guard read clear
- clamp and release paths both exercised under `--dry-run`
- declines to clamp under genuine load — with Chrome and VS Code active
  (busy% 25–100, mean ~47) it correctly stayed at `auto`

**Verified live, installed, against a real game** (Vampire Survivors, app 1794680):

```
auto -> low  (clamp: ema=19% (raw 0%) sclk=7:907Mhz 11.07W)     idle desktop
low -> auto  (release: Steam/game running sclk=0:214Mhz 7.03W)  game launched
auto -> low  (clamp: ema=18% (raw 0%) sclk=7:907Mhz 11.05W)     game closed
```

| Phase | level | sclk | power | guard |
|---|---|---|---|---|
| idle, clamped | `low` | 214 MHz | **7.01 W** | clear |
| game running | `auto` | 907 MHz | 16–17 W | TRIPPED |
| idle again | `low` | 214 MHz | **7.01 W** | clear |

It stayed at `auto` for the whole session (sampled at t+10/20/30 s) and logged no
clamp transition while the game was up. Service came up clean: `NRestarts=0`,
`ExecMainStatus=0`.

**Verified after the compute/latency retune** (2026-08-01, later):

- daemon CPU cost measured on the live process: **0.04 s over 60 s, 0.07 % of a
  core**, down from 21.6 %
- compute guard trips on a real `/dev/kfd` holder and clears when it closes
- worst-case release latency 3 s, down from 10 s
- `install.sh` now actually upgrades a running daemon — `enable --now` silently
  did nothing when the unit was already active, so two earlier "installs" left
  the old code running

**Not verified:**

- **Real-world clamp frequency is unmeasured, and it is the number that decides
  whether this is worth having.** 3.4 W is the saving *while clamped*, not an
  average. In one 118 s window the GPU was clamped 25 % of the time, but that
  window included active terminal work and is not representative.
  `tuning/after.txt` needs a representative day, not a benchmark. The eager
  release settings chosen for compute work will reduce it further.
- The manual `hold`/`unhold` path has not been exercised.
- No GPU compute workload has actually been run against the compute guard —
  only a synthetic `open()` of `/dev/kfd`. There is no OpenCL or ROCm userspace
  installed on this machine, and this GPU is gfx803, which upstream ROCm
  dropped.
- **`ExecStopPost` restoring `auto` is untested.** The logic is there and the
  script's own EXIT trap covers the normal path, but neither has been exercised
  by actually stopping the unit.
- Battery-life impact not measured; the machine has been on AC throughout.
- `IDLE_EMA=20` is a first guess, not a tuned value. The one clamp observed
  fired at ema=19 % and the next at 18 %, which is close enough to the threshold
  that a quieter or busier desktop could behave quite differently.
