# Raw telemetry

Kept because the "before" trace cannot be regenerated once the governor is
installed, the same reason `fan/tuning/` exists.

| File | What |
|---|---|
| `auto-90s-raw.txt` | 45 samples, 2 s apart, idle desktop, Steam closed, `power_dpm_force_performance_level=auto`. Columns: watts, busy%, sclk level. |
| `clamp-probe-raw.txt` | Output of the root probe: `vga_switcheroo` client list, then baseline vs `low`. |

The probe's own baseline block is misleading and is kept deliberately: it reads
7 W at 214 MHz because an earlier probe run had clamped and released moments
before and the GPU had not ramped back up. Five samples were not enough to see
that. `auto-90s-raw.txt` is the honest baseline.

| `after-governor-raw.txt` | 59 samples with the governor installed and running. Columns: watts, busy%, sclk level, dpm setting. Includes both clamped and unclamped periods. |
| `governor-transitions.txt` | Timestamped clamp/release transitions from the journal, including the live game test. |

`after-governor-raw.txt` is **not** a clean "after" trace to set against
`auto-90s-raw.txt` — it spans active terminal work as well as idle, so its mean
of 13.95 W says nothing useful. The comparable numbers are the steady states:
10.38 W at `auto` idle versus 7.01 W clamped. A representative all-day trace is
still missing.
| `mux-switch-refused.txt` | The 2026-08-01 runtime `vga_switcheroo` attempt. Before/after state is identical; the kernel logged `vga_switcheroo: client 1 refused switch`. Kept as the evidence that runtime switching is blocked, not merely risky. |
| `mux-boot-test-refused.txt` | The boot-time attempt, run 11 s into boot before the display manager, with i915 at refcnt=1. Refused identically to the runtime attempt, which is what disproved the "open DRM clients" explanation. |
