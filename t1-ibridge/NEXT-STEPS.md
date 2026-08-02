# Touch Bar: where it stands and what to try next

> **Superseded for practical purposes.** The Touch Bar works, in userspace, with
> no out-of-tree module — see [README.md](README.md) section 3 and
> [scripts/t1-touchbar-daemon.py](scripts/t1-touchbar-daemon.py). Interface 1.2
> was already an ordinary boot keyboard delivering Esc and the F-keys, and
> lighting the panel is one HID feature report on `/dev/hidraw1`. What was
> missing was never the display state — it was the **mode**, which selects the
> layout the T1 draws.
>
> Everything below is now about **upstreaming the driver fixes**, not about
> getting the hardware working. Do not load these modules on a daily driver
> without a deliberate reason; that pursuit is what crashed this machine twice,
> chasing a capability that was already present.

Read `patches/UPSTREAM.md` first for the full evidence. This file is the
forward-looking plan.

## Where it stands

Two bugs found and fixed (both in `patches/`). With them applied, the driver
chain builds and binds correctly on kernel 7.0.0-28-generic:

```
apple-ibridge-hid  0003:05AC:8600.0001   (real interface 1-3:1.2)
                →  0003:1D6B:0301.000B   (virtual touch bar device)
apple-touchbar     0003:1D6B:0301.000B   (bound)
```

The display still does not light, and binding the interface that would fix that
panics the machine ~11 seconds later.

## Order of work

Step 1 is **done** and it answered the whole question — the bar lit from
userspace, so steps 2–6 are no longer needed to get working hardware. They
remain the path to validating the patches for upstream.

| # | Step | Loads modules? | State |
|---|---|---|---|
| 1 | **Diagnostic D** — light the bar from userspace via `hidraw` | no | **done — it works** |
| 2 | **Fix bug 3** — the NULL dereference, then test on 1.2 only | yes | patch `0003` written, untested |
| 3 | **Diagnostic C** — bind 1.3 with `hid-sensor-hub` disabled | yes | not started |
| 4 | **Fix A** — pin the sub-device group, sensor-hub still disabled | yes | not started |
| 5 | Re-enable `hid-sensor-hub`, on its own | yes | not started |
| 6 | **Fix B** — descriptor split, only if step 5 shows a problem | yes | not started |

If the goal is upstreaming, step 2 is the one that matters: the NULL dereference
crashes any T1 machine running that driver, and validating it needs only
interface 1.2 — no need to go near 1.3 or the sensor-hub fight at all.

## The blocker

`apple-touchbar` acts only when it has **both** a mode interface and a display
interface:

```c
if (tb_dev->mode_iface.hdev && tb_dev->disp_iface.hdev && ...)
```

The display controls (Apple usage page `0xFF12`) exist only on interface
`1-3:1.3`:

| hid device | interface | rdesc | contents |
|---|---|---|---|
| `.0001` | `1-3:1.2` | 83 B | boot keyboard only |
| `.0002` | `1-3:1.3` | 634 B | Apple `0xFF12` display page **and** sensor page |

Two separate things go wrong when `1.3` is given to `apple-ibridge`.

### Blocker 1 — a NULL dereference kills the machine (bug 3)

This is what actually crashed it, and **it is live right now in the
configuration this repo calls verified working.**

```c
/* apple-ibridge.c:178 */
for (i = 0; i < ARRAY_SIZE(hdev_info->sub_hdevs); i++) {
        sub_hdev = hdev_info->sub_hdevs[i];
        if (sub_hdev->driver) {          /* no NULL check */
```

Interface `1.2` carries only the keyboard collection, so `sub_hdevs[0]` is filled
and `sub_hdevs[1]` (ALS) stays NULL. Every suspend/resume path walks this loop:

```
BUG: unable to handle page fault for address: 0000000000001c00
Workqueue: pm pm_runtime_work
RIP: 0010:appleib_hid_suspend+0x1f/0x70 [apple_ibridge]
```

The trigger is USB runtime autosuspend — no user action needed; a lid close
reaches it too. `attempt-touchbar.sh` binds 1.2 only, which is exactly this case.
It has survived so far only because `apple-ibridge` holds `hid_hw_open()`, which
inhibits runtime suspend.

**This must be fixed before anything else is attempted.** See
`patches/UPSTREAM.md` for the vmcore evidence and the two related defects
(`appleib_remove_device()` leaving stale pointers; `appleib_ll_parse()` copying
the pre-fixup descriptor).

### Blocker 2 — `hid-sensor-hub` claims the touch bar device

Independent of the crash. `hid-sensor-hub` claims the virtual **touch bar**
device as well as the ALS one:

```
0003:1D6B:0301.000D  driver=hid-sensor-hub    <-- should be apple-touchbar
0003:1D6B:0302.000C  driver=hid-sensor-hub
```

So `apple-touchbar` never receives the device carrying `disp_iface`, and
`tb_dev->active` never becomes true. Candidate fix A below addresses this.

> **Correction.** An earlier revision of this file, and of `UPSTREAM.md`, said the
> crash was `GFP_DMA` exhaustion from two `hid-sensor-hub` instances polling
> through `appleib_ll_raw_request()`, killing `applespi` at ~36 s. The kdump
> vmcore falsifies that: the oops is at +11.4 s, it is the NULL dereference above,
> and that boot recorded **zero** `applespi` errors and **zero** allocation
> failures. Do not plan around the DMA theory.

## Candidate fix A — set the sub-device group explicitly

**A two-line fix for Blocker 2.** Identified and mechanism verified, never tested
on hardware. Not the fix for the crash — see Blocker 1.

`appleib_add_sub_dev()` copies the group from the id table:

```c
sub_hdev->group = dev_id->group;
```

and the table uses `HID_USB_DEVICE`, which — confirmed in
`/usr/src/linux-headers-*/include/linux/hid.h:784` — **never sets `.group`**, so
it stays 0:

```c
#define HID_USB_DEVICE(ven, prod)  .bus = BUS_USB, .vendor = (ven), .product = (prod)
```

`hid_set_group()` only scans the descriptor when the group is unset:

```c
static void hid_set_group(struct hid_device *hdev)
{
	if (hid_ignore_special_drivers) {
		hdev->group = HID_GROUP_GENERIC;
	} else if (!hdev->group &&
	   !(hdev->quirks & HID_QUIRK_HAVE_SPECIAL_DRIVER)) {
		ret = hid_scan_report(hdev);   /* <-- sees sensor collections */
	}
}
```

`hid_scan_report()` assigns `HID_GROUP_SENSOR_HUB` when it finds a
`HID_UP_SENSOR` usage page in a physical or application collection — which the
full 634-byte descriptor contains. So the virtual touch bar device gets
classified as a sensor hub.

**Pre-setting a non-zero group skips the scan entirely.** `HID_DEVICE()` allows
it (`hid.h:782`). Proposed change:

```c
 static struct hid_device_id appleib_sub_hid_ids[] = {
-	{ HID_USB_DEVICE(USB_VENDOR_ID_LINUX_FOUNDATION,
-			 USB_DEVICE_ID_IBRIDGE_TB) },
+	{ HID_DEVICE(BUS_USB, HID_GROUP_GENERIC,
+		     USB_VENDOR_ID_LINUX_FOUNDATION,
+		     USB_DEVICE_ID_IBRIDGE_TB) },
 	{ HID_USB_DEVICE(USB_VENDOR_ID_LINUX_FOUNDATION,
 			 USB_DEVICE_ID_IBRIDGE_ALS) },
 };
```

Why this should work:

- `hid-sensor-hub`'s table requires `HID_GROUP_SENSOR_HUB`, so it can no longer
  match the touch bar device.
- `apple-touchbar`'s table uses `HID_USB_DEVICE`, i.e. group `ANY`, so it still
  matches (confirmed: its modalias is `hid:b0003g*v00001D6Bp00000301` — `g*`).
- The **ALS** sub-device is deliberately left alone, so it still gets scanned,
  still becomes `HID_GROUP_SENSOR_HUB`, and `hid-sensor-hub` still binds it.
  The light sensor should keep working.

This addresses Blocker 2 only. **It does nothing about the NULL dereference**,
which is what actually crashes the machine — fix that first.

**Assumptions, now checked:**

- ✅ `hid_set_group()` in 7.0.0-28 is byte-identical to the version quoted above.
  Verified by streaming `drivers/hid/hid-core.c` out of
  `/usr/src/linux-source-7.0.0/linux-source-7.0.0.tar.bz2` (`:2763`).
- ✅ `hid_add_device()` calls `parse()`, rejects on `!dev_rdesc` (`:2975-2979`),
  **then** calls `hid_set_group()` (`:2984`). Since `appleib_add_sub_dev()` sets
  `sub_hdev->group` before `hid_add_device()`, a pre-set group does survive and
  does skip the scan.
- ✅ `hid_ignore_special_drivers` is 0. Not readable without root, but inferable:
  if it were 1, `hid_set_group()` would force `HID_GROUP_GENERIC` on everything
  and `hid-sensor-hub` could never bind — and it is bound right now.
- ✅ Nothing in `apple-touchbar` depends on the group being `ANY`.
  `HID_GROUP_ANY == 0` is a wildcard on the *id table* side in
  `hid_match_one_id()`; setting the *device's* group does not affect it.

Still unverified: whether the ALS sub-device behaves once it rides a virtual
device rather than the real interface. Today's working ALS binds
`hid-sensor-hub` directly to real interface `1.3`; A moves it to virtual
`1D6B:0302`. That configuration has never run. There is no longer a specific
predicted failure mode for it, so watch broadly rather than for one metric.

## Candidate fix B — split the report descriptor per sub-device

The thorough fix, and what upstream probably wants regardless of whether A
works: `appleib_add_sub_dev()` should give each virtual sub-device only the
collections that belong to it, instead of copying the whole descriptor.

Sketch:

1. Walk the raw descriptor with a minimal HID item parser (short items: high 4
   bits = tag, next 2 = type, low 2 = size).
2. Track top-level application collections (`0xA1 0x01`) and the Usage Page /
   Usage that precede each.
3. Emit into the per-sub-device descriptor only the top-level collections whose
   usage maps to that sub-device via `appleib_usage_map`, preserving the global
   items in scope.
4. Keep report IDs intact — `appleib_hid_raw_event()` forwards every report to
   every open sub-device and relies on IDs to sort it out.

Roughly 150–250 lines. Check whether `hid_input_report()` errors or merely
ignores a report ID absent from a sub-device's descriptor; if it errors, the
forwarding in `appleib_hid_raw_event()` needs a matching filter.

## Diagnostic C — bind 1.3 with `hid-sensor-hub` disabled

With `hid-sensor-hub` unloadable, nothing can claim `1D6B:0301.000D`, so
`apple-touchbar` gets it and `disp_iface` populates. One variable, and it needs
no rebuild — so it comes **before** fix A, not after.

Use `install hid_sensor_hub /bin/false`, **not** `blacklist`. `hid_sensor_als`,
`hid_sensor_trigger` and `hid_sensor_iio_common` are all loaded and all depend on
it, and a dependency load bypasses `blacklist` entirely. Verify post-boot with
`lsmod` that all four are absent, and that
`/sys/bus/hid/devices/0003:05AC:8600.0002/driver` is gone, before proceeding.

Answers: does `apple-touchbar` light the display once it has both interfaces?

Cost: the ambient light sensor stops working until reverted. Temporary.

## Diagnostic D — light the bar from userspace — DONE, and it worked

`/dev/hidraw1` already exists on real interface `1-3:1.3` (`hid-sensor-hub` binds
with `HID_CONNECT_DEFAULT`, which includes `HID_CONNECT_HIDRAW`), so the display
report can be driven with nothing loaded and nothing unbound.

Confirmed layout, decoded from the live descriptors:

```
1-3:1.2   83 B   app 00010006   Output  report 2: 00FF0004 @byte 0   (mode)
1-3:1.3  634 B   app FF120001   Feature report 2: FF120020 @byte 0   (aux1)
                                                  FF120021 @byte 1   (disp)
```

The first `GET_FEATURE` was the surprise:

```
02 05 01 00 00 00 00 e8 03 00 00
      ^^ disp = 1 = ON, while the panel was dark
```

The display state had been "on" all along. What was missing was the **mode**,
which selects the layout the T1 renders; with none set it draws nothing. Sending
output report 2 with value 1 (`fn`) to `/dev/hidraw0` lit the bar immediately.

Two further findings:

- **The class request works.** `appletb_set_tb_mode()` goes out of its way to
  avoid it on T1, falling back to a vendor `usb_control_msg()` with no report-ID
  prefix (apple-touchbar.c:250). A plain class output report `[0x02, 0x01]` via
  hidraw worked here, so that quirk may be unnecessary — worth mentioning if the
  patches are sent upstream.
- **The keys were never broken.** Interface 1.2 is registered at `event4` with
  the `kbd` handler under `hid-generic`. Watching the raw reports shows
  `01 00 00 29 ...` for Esc (HID usage `0x29`) and the terminal echoing `^[`.
  Esc and F1–F12 had been arriving the whole time.

## Testing protocol — non-negotiable

1. **Ask the user first**, and tell them to save their work.
2. **Fix the NULL dereference before loading anything again.** The current
   1.2-only configuration is one lid close away from the same oops.
3. **Fresh boot every time.** Never load after a previous attempt in the same
   boot. Keep the guard in `scripts/attempt-touchbar.sh`.
4. **Never `rmmod`** — and note that sysfs unbind is *not* a safe alternative;
   it reaches `appleib_hid_remove → hid_destroy_device` by the same inner path.
   Treat unbind as untested until the teardown fixes land.
5. **Disable USB autosuspend on the T1** (`power/control = on`) and disable
   lid/idle suspend. This is the actual crash trigger, and nothing in the current
   scripts guards it.
6. **Unplug the dock.** It shares the Thunderbolt controller, which oopsed
   independently once.
7. **Keep an escape hatch**: the external USB keyboard (`045e:074b`, bus 1, plain
   `usbhid`) is independent of `applespi` and the T1, and gives Alt+SysRq. An SSH
   session from a second machine is better still.

**Recovery is already automatic.** `kernel.panic_on_oops=1` and kdump is armed:
both crashes panicked, kexec'd, wrote a vmcore to `/var/crash`, and rebooted
unattended in ~2.5 minutes. No power cycle was involved — if one had been, there
would be no vmcore. What looked like "a hard lock with no journal output" was a
panic; panics do not flush the journal, so **read `/var/crash`, not the journal**.

One real gap: `kernel.panic = 0`, so a panic that fails to kexec hangs forever.
Set `kernel.panic = 60` as a backstop. `kernel.sysrq = 176`, so `s`/`u`/`b`
already work from a pre-opened `/proc/sysrq-trigger` fd.

## Also worth knowing

- The `-ENODEV` failures that hid the second bug for so long are **silent** by
  default. The `hid_err()` added in patch 1 is what made it diagnosable; keep
  that kind of instrumentation in place.
- Even if the display lights up, expect the T1 to render only predefined
  layouts (Esc, F-keys, media keys). Arbitrary content is a separate problem —
  that is what `appletbdrm` does for the T2, and there is no T1 equivalent.
- This model has **no physical Esc key**, so a working Touch Bar has real
  practical value beyond novelty.
