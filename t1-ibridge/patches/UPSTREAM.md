# apple-ibridge on modern kernels: three bugs, four patches, and where it still dead-ends

Findings from debugging the Apple T1 (iBridge, `05ac:8600`) Touch Bar on a
MacBookPro14,3 running kernel **7.0.0-28-generic**, against
[`t2linux/apple-ib-drv`](https://github.com/t2linux/apple-ib-drv) branch `mbp15`
(commit `4afd309`, Jan 2025).

Relevant to [t2linux/kernel issue #12](https://github.com/t2linux/kernel/issues/12),
"Touch bar broken on T1 macs since Linux 6.3" — though the conclusion below is
that the commit named in that issue is **not** the primary cause.

## Summary

The driver builds cleanly on 7.0 with no API drift, and its device table does
match the T1. Three independent bugs in `apple-ibridge.c` prevent it from working:

1. **An out-of-bounds write** when creating sub-devices, which corrupts kernel
   memory. Present regardless of kernel version. Patch `0001`.
2. **`appleib_ll_parse()` never populates `dev_rdesc`**, so every virtual
   sub-device is rejected with `-ENODEV` by `hid_add_device()`. This is the
   regression that actually broke the Touch Bar on modern kernels. Patch `0002`.
3. **A NULL dereference in `appleib_forward_int_op()`**, hit on every suspend and
   resume path whenever any sub-device slot is unfilled — which is the normal case
   for interface `1.2`. Triggered by USB runtime autosuspend, so it needs no user
   action at all. Patch `0003`. **Not yet tested on hardware.**

Patch `0004` additionally corrects the descriptor that patch `0002` copies —
`dev_rdesc` is the pre-`report_fixup()` version, so sub-devices were being handed
the two invalid 64-bit fields that `appleib_report_fixup()` exists to split.

With 1 and 2 fixed, `apple-ibridge` creates the virtual `1d6b:0301` device and
`apple-touchbar` binds to it. The display still does not light, because
`apple-touchbar` also needs the display interface, which lives on `1.3` — and
binding `1.3` runs into bug 3 plus a separate driver-matching problem, both
described at the end.

An earlier revision of this document blamed `GFP_DMA` exhaustion for the crash.
That was wrong; the correction and the vmcore evidence are in the last section.

## Bug 1 — out-of-bounds write in `appleib_add_device()`

`hdev_info->sub_hdevs[]` is sized `ARRAY_SIZE(appleib_sub_hid_ids)` = **2**, but
the loop indexes it with `i`, which runs to `hdev->maxcollection`. On a real
iBridge that exceeds 2, so the loop writes past the array into the `sub_open[]`
member that follows it in `struct appleib_hid_dev_info`.

UBSAN catches the corrupted `bool` being read back in `appleib_hid_raw_event()`:

```
UBSAN: invalid-load in apple-ibridge.c:102:7
load of value 122 is not a valid value for type '_Bool'
  appleib_hid_raw_event.cold+0x19/0x29 [apple_ibridge]
  __hid_input_report+0x142/0x240 [hid]
  hid_irq_in+0x208/0x240 [usbhid]
```

`122` is a byte out of a `hid_device *`.

There is a second indexing error in the same loop. `appleib_usage_map` maps
**four** usages onto **two** sub-devices — three of them (`0x00010006`,
`0x000D0005`, `0xFF120001`) to the touch bar. Indexing by collection therefore
also attempts to create the same virtual device once per matching collection.

The error path is wrong too:

```c
while (i-- > 0)
        hid_destroy_device(hdev_info->sub_hdevs[i]);
return (void *)hdev_info->sub_hdevs[i];
```

That loop always exits with `i == -1`, so the return reads `sub_hdevs[-1]`:

```
UBSAN: array-index-out-of-bounds in apple-ibridge.c:416:40
index -1 is out of range for type 'hid_device *[2]'
  appleib_hid_probe+0x347/0x350 [apple_ibridge]
```

**Fix:** index by sub-device (`dev_id - appleib_sub_hid_ids`), skip a
sub-device an earlier collection already created, and repair the error path.
See `0001-*.patch`.

> **Warning for anyone reproducing this.** Once the unpatched driver has run,
> kernel memory is already corrupt. Running `rmmod` then calls
> `hid_destroy_device()` on garbage pointers, which faulted inside `kobject_put`
> and panicked the test machine. **Reboot instead of unloading.** Note that a
> sysfs unbind is *not* a safer alternative: `device_release_driver_internal →
> hid_device_remove → appleib_hid_remove → hid_destroy_device` is the same inner
> path, differing only in the outer caller.

## Bug 2 — `appleib_ll_parse()` leaves `dev_rdesc` NULL

This is the one that broke the Touch Bar on newer kernels.

```c
static int appleib_ll_parse(struct hid_device *hdev)
{
        /* we've already called hid_parse_report() */
        return 0;
}
```

That comment was true when the driver was written. It isn't now.
`hid_add_device()` calls the callback itself and then requires the descriptor:

```c
ret = hdev->ll_driver->parse(hdev);
if (ret)
        return ret;
if (!hdev->dev_rdesc)
        return -ENODEV;
```

So every virtual sub-device fails with `-ENODEV`, `hid_add_device()` returns
early, and neither the touch bar nor the ALS sub-device is ever created. Nothing
is logged by default — the failure is entirely silent unless you instrument the
caller, which is why it appears as "the touch bar just does nothing".

Observed with the diagnostic added in patch 1:

```
apple-ibridge-hid 0003:05AC:8600.0001: ib: failed to add sub-device 0 for usage 10006: -19
```

**Fix:** give the sub-device a copy of the parent's report descriptor, which is
what the `ll_driver` is emulating anyway. See `0002-*.patch`.

### Why this matters for issue #12

Issue #12 attributes the breakage to commit `e04955db6a7c` ("HID: Recognize
sensors with application collections", 6.3), which lets `hid-sensor-hub` claim
the real interface by application collection. That commit is real and does
matter — see below — but it is not why the touch bar stopped working. Even with
`apple-ibridge` bound to the interface, **no virtual device could ever be
created**, because of Bug 2. Reverting `e04955db6a7c` alone would not fix it.

## What works after both patches

On a MacBookPro14,3, kernel 7.0.0-28-generic:

```
apple-ibridge-hid  0003:05AC:8600.0001   (real interface 1-3:1.2)
                →  0003:1D6B:0301.000B   (virtual touch bar device, CREATED)
apple-touchbar     0003:1D6B:0301.000B   (BOUND)
```

No UBSAN, no oops, camera and ambient light sensor unaffected.

## Where it still dead-ends

The Touch Bar display does not turn on, because `apple-touchbar` acts only when
it has both a mode interface and a display interface:

```c
if (tb_dev->mode_iface.hdev && tb_dev->disp_iface.hdev && ...)
```

The display controls live in the Apple `0xFF12` usage page, which is present
**only** in the 634-byte descriptor on interface `1-3:1.3`. Interface `1-3:1.2`
is an 83-byte boot keyboard:

| hid device | interface | rdesc | contents |
|---|---|---|---|
| `.0001` | `1-3:1.2` | 83 B | boot keyboard only |
| `.0002` | `1-3:1.3` | 634 B | Apple `0xFF12` display page **and** sensor page |

(634 is also exactly the size `appleib_report_fixup()` special-cases, so this is
the descriptor the driver was written against.)

So interface `1.3` must also go to `apple-ibridge`. Doing that panics the machine
about eleven seconds later — but **not** for the reason this document previously
claimed. See "Bug 3" below; the earlier `GFP_DMA`-exhaustion account was wrong and
is corrected at the end of this file.

There is a second, independent problem with binding `1.3`. `apple-ibridge` hands
every virtual sub-device the *entire* 634-byte descriptor, so `hid_set_group()`
sees sensor collections in it and classifies the virtual **touch bar** device as a
sensor hub:

```
0003:1D6B:0301.000D  driver=hid-sensor-hub    <-- should be apple-touchbar
0003:1D6B:0302.000C  driver=hid-sensor-hub
```

The mechanism is confirmed in this kernel's source. `appleib_add_sub_dev()` does
`sub_hdev->group = dev_id->group`, and `appleib_sub_hid_ids[]` uses
`HID_USB_DEVICE()`, which never sets `.group` (`hid.h:784`), so it stays 0.
`hid_add_device()` then calls `hid_set_group()` (`hid-core.c:2984`), which scans
the descriptor whenever the group is unset:

```c
/* hid-core.c:2763 */
} else if (!hdev->group && !(hdev->quirks & HID_QUIRK_HAVE_SPECIAL_DRIVER))
        ret = hid_scan_report(hdev);
/* hid-core.c:861 — any HID_UP_SENSOR collection sets HID_GROUP_SENSOR_HUB */
```

**This is where `e04955db6a7c` genuinely bites** — not on the real interface, but
one level deeper, on the virtual device the driver synthesises.

A two-line fix is available and its mechanism checks out, though it is untested on
hardware: pre-set a non-zero group on the touch bar entry so the scan is skipped
entirely.

```c
-	{ HID_USB_DEVICE(USB_VENDOR_ID_LINUX_FOUNDATION, USB_DEVICE_ID_IBRIDGE_TB) },
+	{ HID_DEVICE(BUS_USB, HID_GROUP_GENERIC,
+		     USB_VENDOR_ID_LINUX_FOUNDATION, USB_DEVICE_ID_IBRIDGE_TB) },
```

`hid-sensor-hub`'s table requires `HID_GROUP_SENSOR_HUB`, so it can no longer
match; `apple-touchbar`'s entries use `HID_USB_DEVICE`, i.e. group `ANY`
(`HID_GROUP_ANY == 0` is a wildcard in `hid_match_one_id()`), so it still does.
Leaving the ALS entry alone keeps that sub-device classified as a sensor hub.

The thorough alternative is for `appleib_add_sub_dev()` to give each virtual
sub-device only the portion of the report descriptor belonging to it, rather than
copying it wholesale. That is a substantially larger change, and it wants testing
on hardware that can be crashed freely.

## Bug 3 — NULL dereference in `appleib_forward_int_op()`

This is what actually killed the machine, and it is present regardless of which
interfaces are bound.

```c
/* apple-ibridge.c:178 */
for (i = 0; i < ARRAY_SIZE(hdev_info->sub_hdevs); i++) {
        sub_hdev = hdev_info->sub_hdevs[i];
        if (sub_hdev->driver) {          /* no NULL check */
```

`appleib_add_device()` only fills the slots whose usages appear in that
interface's descriptor. Interface `1.2` carries just the keyboard collection, so
`sub_hdevs[0]` is set and `sub_hdevs[1]` (ALS) stays **NULL**. Every suspend and
resume path runs through this loop, so any of them faults:

```
BUG: unable to handle page fault for address: 0000000000001c00
Oops: 0000 [#1] SMP PTI
Workqueue: pm pm_runtime_work
RIP: 0010:appleib_hid_suspend+0x1f/0x70 [apple_ibridge]
 hid_suspend+0xd2/0x260 [usbhid]
 pm_runtime_work+0x99/0xa0
```

The trigger is **USB runtime autosuspend** — automatic, requiring no user action.
A lid close reaches it too.

**Fix:** skip NULL slots — patch `0003`, which also bails out when
`hid_get_drvdata()` returns NULL, since these paths can run after
`appleib_remove_device()` has cleared it. Two related defects in the same area:

- `appleib_remove_device()` (:472) calls `hid_destroy_device()` but leaves the
  pointers non-NULL, which is the likely source of the `kobject_put` refcount
  fault seen during teardown. Also patch `0003`.
- `appleib_ll_parse()` (patch 2 above) copies `parent->dev_rdesc`. `hid-core.c`
  captures `dev_rdesc` at line 1009, *before* `report_fixup()` runs at line 1299
  and assigns the fixed version to `rdesc` at :1312. So sub-devices receive the
  **un-fixed** descriptor, re-introducing the two invalid 64-bit fields that
  `appleib_report_fixup()` exists to split. Patch `0004` copies `parent->rdesc`
  and `parent->rsize` instead.

### Status of patches 0003 and 0004

Both build cleanly against 7.0.0-28 and are committed on top of `4afd309`, but
**neither has been loaded on hardware yet.** The next test is the cheapest
possible reproduction: bind interface `1.2` only — the configuration where the
ALS slot is NULL — and deliberately provoke runtime autosuspend. That is the
exact path that produced the oops above, and it validates patch `0003` without
going near interface `1.3`.

## Correction: the earlier DMA-exhaustion account was wrong

An earlier revision of this document attributed the crash to `GFP_DMA` exhaustion:
two `hid-sensor-hub` instances polling through `appleib_ll_raw_request()`, each
transfer taking a DMA buffer, the zone exhausted in ~36 s, `applespi` then failing
with `-ENOMEM`. **That is not what happened.** It is recorded here because the
reasoning is a useful trap to avoid.

Both crashes wrote kdump vmcores (`kernel.panic_on_oops=1`, kdump armed) to
`/var/crash`, and they had not been read. Decoding the dmesg from
`/var/crash/202607311851` shows:

- The `1.3` bind at monotonic **419.048**, the oops at **430.485** — 11.4 s, not 36.
- The oops is the NULL dereference above, on the `pm` workqueue.
- In that entire boot: `applespi ... Error reading` = **0**, `SLUB: Unable to
  allocate` = **0**. There was no memory pressure of any kind.

The quoted `applespi -12` flood and `gfp=0xdc1(...|GFP_DMA|...)` line came from a
different boot, not the one where `apple-ibridge` was loaded.

The mechanism was never plausible on its own terms either: in-flight USB buffers
are freed on completion, so per-transfer allocation cannot drain a zone without a
leak, and a steady monotonic decline is a leak signature rather than a transfer
signature. That should have been the tell.

Two lessons worth carrying: read the vmcore before theorising, and note that what
was experienced as a "hard lock with no journal output" was a panic followed by an
automatic kexec, dump and reboot — the journal is empty because panics do not
flush, and the evidence went to `/var/crash` instead.

## Reproducing

```sh
git clone --branch mbp15 https://github.com/t2linux/apple-ib-drv
cd apple-ib-drv
git am /path/to/000*.patch
make
sudo insmod ./apple-ibridge.ko
sudo insmod ./apple-touchbar.ko
```

Also required on Ubuntu: `usbmuxd` claims `05ac:8600` and unconfigures it via
`/lib/udev/rules.d/39-usbmuxd.rules` (`ATTR{bConfigurationValue}="0"`). Override
that first or the device has no interfaces at all — see `../udev/39-usbmuxd.rules`.

**Do not `rmmod`.** Reboot between attempts.

## Note before submitting

The patches carry no `Signed-off-by:` line. Kernel contributions require one
with a real name under the Developer's Certificate of Origin, so add:

```
Signed-off-by: Your Real Name <your@email>
```

to both before sending. `git commit --amend -s` / `git rebase --signoff` will do
it. Also consider Cc'ing Ronald Tschalär, the original author.
