# Fan profile (mbpfan)

The stock `mbpfan` thresholds made the fan surge for a second or so about once a
minute during ordinary work — the classic "why is this laptop breathing at me"
problem, and very unlike how the same hardware behaves under macOS.

## Root cause

`mbpfan` 2.4.0 reads the **instantaneous** maximum core temperature every poll,
with no filtering or averaging. On this i7-7920HQ the package temperature swings
**64 °C → 94 °C within a single second** on turbo bursts.

The stock configuration was `low_temp=63, high_temp=66, max_temp=86` at
`polling_interval=1`. Every one of those numbers sits inside the normal working
range:

- `high_temp` (66) was **below the idle floor**, so the fan was essentially
  always ramping.
- `max_temp` (86) sat inside the transient spike range, so a sub-second burst
  hit the snap-to-maximum branch and commanded full speed.

Measured over a representative window, the fan was at **full speed 50 % of the
time** and never once at minimum.

## Fix

```
low_temp  = 75      # below this, fans idle at minimum
high_temp = 82      # only begin ramping above this
max_temp  = 99      # full speed; just under Tjmax (100)
polling_interval = 7
```

Raising `max_temp` close to Tjmax does double duty: sub-second spikes no longer
reach the snap-to-maximum branch, **and** it stretches the ramp curve so each
step is roughly 25 rpm instead of ~180. The longer poll interval filters the
sub-second spikes that caused the surging in the first place.

## Result

| Metric | Before (63/66/86 @ 1s) | After (75/82/99 @ 7s) |
|---|---|---|
| Time at minimum speed | 0 % | **78 %** |
| Time at full speed | 50 % | **3 %** |
| Total rpm travelled | 66,006 | **12,766** |

Raw before/after telemetry is in [tuning/](tuning/). The "before" trace can't be
regenerated without reverting the config, so it's kept deliberately.

## 2026-08-01 revision: the profile above optimised for the wrong thing

The 75/82/99 profile succeeded at what it was written for and is kept above
because the reasoning still holds. But it traded temperature for silence, and
when the goal changed to preserving the machine, measurement showed the cost:

| | 75/82/99 |
|---|---|
| package mean | 64.7 °C |
| package peak | **88 °C — with no fan response at all** |
| fan1 | ~2090 rpm, sitting on its 2160 floor |

Ramping did not begin until 82 °C, so an 88 °C spike passed unanswered and the
entire 2160–5927 rpm range went unused. Revised to **65/75/95**, keeping
`polling_interval = 7`. The band between `low` and `high` is where ordinary work
lives; in it the fan simply holds rather than snapping to minimum, which is what
prevents a return to surging.

## CPU sustained power cap

RAPL reported **`long_term = 100 W` on a 45 W part** — no meaningful sustained
cap, so the CPU could make heat indefinitely.
[`cpu-power-limit`](cpu-power-limit) sets PL1 to 40 W and leaves PL2 at 125 W,
so bursty interactive work rides the short-term limit unchanged and only long
all-core loads are bounded. RAPL resets each boot, hence the service.

```
PL1 100W -> 40W (PL2 unchanged at 125W)
```

The script reads the value back and fails loudly rather than assuming, because
firmware can silently lock these registers.

## Does the cap work? Yes — the first measurement of it was wrong

The first load test recorded `pkg mean 97.7 C | peak 100 C` and concluded that
the machine reaches Tjmax *despite* a 40 W cap, therefore the cooling system
simply cannot move the heat. **That conclusion was built on an unverified
assumption and is withdrawn.**

PL1 was set and read back at install, but never checked *during* the run. It was
later found at 100 W with the service never having re-run, so the load test that
hit Tjmax was very likely running uncapped.

Re-run with PL1 sampled every 2 s alongside temperature:

| | first test (PL1 unverified) | re-run (PL1 verified) |
|---|---|---|
| PL1 during load | assumed 40 W, probably 100 W | **33–42 W, mean 39.1 W** |
| package mean | 97.7 °C | **94.0 °C** |
| package peak | **100 °C — Tjmax** | **96 °C — never reached Tjmax** |
| fan1 peak | 5714 rpm | 5931 rpm (at maximum) |

So the cap does work and it does help: roughly 4 °C off the peak, and enough to
keep the CPU off its thermal limit entirely.

### thermald is a second writer, but only under stress

`thermald` actively modulates PL1 — the re-run shows it moving between 33 W and
42 W rather than sitting at the 40 W written. At idle it does not touch it at
all: with the cap set to 40 W, PL1 held for 36 s both with `thermald` running
and with it stopped.

That is why the earlier revert to 100 W went unnoticed. `thermald` is quiescent
when cool and takes over when hot, and it appears to restore the firmware
default once temperatures fall. **Treat our 40 W as a starting point that
`thermald` then modulates, not as a fixed ceiling.** The two coexist; `thermald`
has the final say under load, which is the correct order of authority.

### What the cooling evidence actually supports

A weaker claim than the withdrawn one, but still worth acting on: **94 °C mean
with the fans pinned at maximum, on a load drawing under 40 W.** A healthy
i7-7920HQ at 40 W should sit well below that. The machine no longer saturates at
Tjmax, but it is working extremely hard to shed a modest load.

That is consistent with degraded thermal paste and dust-blocked fins on a 2017
chassis — and this machine additionally suffered a water spill, so corrosion is
a live possibility that a strip-down would reveal. A repaste and clean remains
the highest-value action for longevity; the case is now built on "working far
too hard" rather than "cannot cope at all."

Raw traces in [tuning/](tuning/).

## Install

```sh
sudo ./install.sh
```

Installs the fan profile and the power cap together, restarts `mbpfan`, enables
`cpu-power-limit`, and reads both values back. To revert the cap alone:

```sh
sudo systemctl disable --now cpu-power-limit   # ExecStop restores PL1
```

## Status

**Verified:**

- PL1 write took effect, confirmed by read-back: 100 W → 40 W, PL2 untouched
- the cap **holds under load** — sampled every 2 s through an all-core run it
  stayed between 33 W and 42 W, mean 39.1 W
- the cap **holds at idle** with `thermald` both running and stopped
- the fan now responds — **5931 rpm**, its maximum, under load, and it holds at
  ~2445 rpm during light work instead of dropping to the 2160 floor
- with the cap verifiably in effect the package peaks at **96 °C and does not
  reach Tjmax**

**Withdrawn:** the earlier claim that the machine reaches Tjmax *despite* the
40 W cap. PL1 was never sampled during that run and was later found at 100 W, so
it was very likely uncapped. See the section above.

**Not verified:**

- Actual sustained package power in watts. `energy_uj` is root-only, so the cap
  is verified as *applied* (PL1 sampled at 33-42 W throughout a load) but the
  real draw was never measured directly.
- Why PL1 was found at 100 W after the first load test. `thermald` restoring the
  firmware default once cool is the best explanation, but it was not caught in
  the act.
- **Whether the new fan profile lowers idle temperature.** The before/after
  idle traces are not comparable — load was 1.07 against 1.84, 72 % higher — so
  the 64.7 → 66.1 °C difference cannot be attributed to the change. A matched
  comparison has not been run.
- No before/after under the old profile at matched load, so the fan revision's
  benefit under load is inferred from the mechanism, not measured.
- Whether the noise increase is acceptable in daily use.

## Known residual

Roughly one 7-second full-speed burst every few minutes, from a sub-second spike
crossing `max_temp`. A real fix would be upstream of the fan controller — capping
turbo (`max_perf_pct` ≈ 75) or reducing DisplayLinkManager's constant ~14 % CPU,
which only runs while the dock is attached. Not pursued; the current profile is
already close to inaudible in normal use.

Safety note: these thresholds are higher than stock, which is the entire point,
but they remain below Tjmax and the hardware throttles independently regardless.
