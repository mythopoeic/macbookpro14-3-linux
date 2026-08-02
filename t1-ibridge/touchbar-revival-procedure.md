# T1 Touch Bar Revival — External macOS Route

**Machine:** MacBookPro14,3 (15-inch, 2017) running Ubuntu
**Goal:** get the Apple T1 chip booting again, which restores the Touch Bar, Touch ID, ambient light sensor, and the built-in FaceTime camera
**Risk to your Ubuntu install:** none, if you follow the warnings below

---

## Why this is the approach

The T1 is alive but sitting in Recovery mode with no OS to load. Verified on the machine:

```
MODE: Recovery
CPID: 0x8002        (T1 silicon)
ECID: 0x0008e98e1084e326
SRTG: N/A           (no boot image)
```

Everything behind the T1 is gone together — Touch Bar, Touch ID, and the FaceTime camera all hang off that one chip. It is one fault, not several.

Apple publishes standalone firmware images (IPSWs) only for **iBridge2,x**, which is the T2 chip. The T1 is **iBridge1,1** and appears nowhere in Apple's firmware manifest. That rules out `idevicerestore` and Apple Configurator — not because the tools fail, but because there is no image to feed them.

T1 firmware is delivered **only by macOS**, through an EFI updater staged on the boot partition and run at startup. The remains of that machinery are still in this Mac's NVRAM, pointing at files that no longer exist:

```
Boot0003* Mac OS X → \EFI\APPLE\UPDATERS\MULTIUPDATER\MultiUpdater.efi
```

So: install macOS on an external drive, let it stage and run that updater, and see whether the T1 comes back.

**Honest expectation:** this may not work. Whether macOS running from an external disk stages a firmware payload for the internal T1 could not be established from any source. It is worth trying first because it risks nothing, while the fallback starts by repartitioning your root filesystem.

---

## What you need

- The new **PNY 233 GB flash drive** on its A-to-C adapter (only ~60 GB gets used)
- Wi-Fi, and the power adapter plugged in the whole time
- 2–3 hours, mostly unattended

Tested and confirmed: it runs at SuperSpeed Plus 10 Gbps with UAS, in either connector orientation, on either right-hand port.

The WD easystore is **not** being used for this. It keeps its data and stays out of the procedure entirely.

---

## Read before you start

**1. Unplug everything except the new SSD.** The dock and the WD easystore both come off. Fewer disks attached means fewer disks you can select by mistake — and Mac firmware will not reliably boot from a drive behind a DisplayLink dock anyway.

**2. Plug the drive into one of the RIGHT-HAND USB-C ports.** Tested 2026-07-31: the left-hand pair does not work for USB at all — two different drives, two different cables, both orientations, zero response every time. Only the right-hand ports enumerate (they appear as bus 6 in Linux). The drive runs at full SuperSpeed Plus 10 Gbps there, in either orientation.

Those ports share a controller with the dock, which has a known wedging fault — but with the dock unplugged the drive is the only device on it, which is the safe configuration.

**3. Two disks must never be selected. Know them by size:**

| Disk | Size | What it is |
|---|---|---|
| Internal | **465 GB** — `APPLE SSD SM0512L` | Ubuntu. Erasing it destroys your system. |
| WD easystore | **931 GB** — `WD10SDRW` | Your `Fehringer` data. |
| PNY flash drive | **233 GB** — `USB 3.2.2 FD` | **This is the only correct target.** |

If the sizes are ambiguous, stop and check rather than guess.

**4. macOS cannot read ext4, so your Ubuntu disk will look broken to it.** You may get a popup saying a disk is unreadable and offering to **Initialize**. Click **Ignore**. Never Initialize. The same may happen for the WD's NTFS partition — also Ignore.

**5. Double-check the target every time you erase or install.** Confirm the drive by *name and size* before clicking anything.

---

## Steps

### 1. Prepare
Unplug the DisplayLink dock **and the WD easystore**. Plug the new drive into a **right-hand** USB-C port — the left pair does not work. Connect the power adapter. Shut down Ubuntu fully.

With only one external disk attached, the "which drive do I pick" question in step 3 answers itself.

### 2. Boot into Internet Recovery
Power on and **immediately hold `Command` + `Option` + `R`**. Keep holding until a spinning globe appears, then let go.

The machine will ask for Wi-Fi, then download a recovery image. This takes a while — the progress bar is often wrong. Let it run.

*If the globe never appears or recovery fails to load:* stop here and report back. Apple's recovery servers are unreliable for older models, and the workaround needs a second Mac.

### 3. Erase the external drive
Open **Disk Utility**.

- In the menu bar: **View → Show All Devices** (important — without this you cannot see the whole drive)
- Select the **external drive** in the left sidebar — confirm the name and size
- Click **Erase**
- Name: `macOS`
- Format: **APFS**
- Scheme: **GUID Partition Map**
- Click Erase, then quit Disk Utility

### 4. Install macOS
Choose **Reinstall macOS** from the recovery menu. When it asks where to install, select the **`macOS` volume on the external drive**.

Takes 30–60 minutes and reboots several times on its own. Do not hold any keys during those reboots. If it boots back into Ubuntu at some point, restart while holding `Option` and pick the external drive to continue.

### 5. Finish setup
Work through Setup Assistant. Skip Apple ID and skip Touch ID — Touch ID cannot work yet.

### 6. First check — does the Touch Bar light up?

This is the immediate tell. In macOS, look at the Touch Bar.

- **Lit up and responding** → the T1 is alive. Excellent.
- **Still dark** → keep going anyway. Step 7 is where firmware updates actually get delivered.

### 7. Run Software Update until it's exhausted
**System Settings → General → Software Update.** Install everything offered. Reboot. Check again. Repeat until it reports no updates remaining.

This step matters more than the install itself — firmware updates ride along with macOS updates, and this is what runs the T1 updater. Do not skip it, and do not stop after one round.

### 8. Second check — back in Ubuntu
Restart holding `Option`, choose the Ubuntu drive, and run:

```
lsusb | grep 05ac
```

- `05ac:8600` — **SUCCESS.** The T1 is booting. Touch Bar and camera are now reachable from Linux.
- `05ac:1281` — still in Recovery. The external route did not work.

Useful extra detail either way:

```
lsusb -d 05ac:8600 -v 2>/dev/null | head -20
ls -l /dev/video*
```

---

## Getting back into Ubuntu

Hold **`Option`** at power-on and pick the Ubuntu drive. macOS may have made itself the default boot device; that is easy to change back from Ubuntu afterward and is not a problem.

---

## What happens next

**If it worked** (`05ac:8600`):
The FaceTime camera should appear on its own — it is a standard USB video device and `uvcvideo` drives it with no extra work. The Touch Bar needs three out-of-tree DKMS drivers: `apple-ibridge`, `apple_ib_tb`, `apple_ib_als`. None are installed yet. That is all shell work and can be handled for you.

**If it did not work** (`05ac:1281`):
Do not erase anything, and do not repartition on your own. The fallback is installing macOS to a ~60–80 GB partition on the internal disk, which requires a full backup, an Ubuntu live USB, and an offline filesystem shrink — worth planning properly rather than improvising.

Either way, report the `lsusb` output.

---

## Expected, not problems

- Touch Bar stays dark through the whole install
- Touch ID does not work
- The internal disk shows up as unreadable or uninitialized in macOS
- Fans run loud during installation
- Progress bars lie, repeatedly
