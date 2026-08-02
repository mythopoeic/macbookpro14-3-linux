#!/usr/bin/env python3
"""Remember and restore GNOME monitor layouts per dock + monitor combination.

Why this exists
---------------
GNOME already saves monitor layouts, in ~/.config/monitors.xml. It does not
work with a DisplayLink dock, and the reason is specific:

  * Each DisplayLink output is a separate DRM card created by `evdi`, named
    DVI-I-1, DVI-I-2, ... in the order the outputs bind. That order is not
    stable across hot-plugs.
  * mutter keys a saved configuration on the full monitor spec, and the spec
    includes the connector name. Swap the connectors and the saved config no
    longer matches.

The observed result on this machine was two configurations in monitors.xml for
the same three physical monitors, differing only in which ASUS panel got
DVI-I-1. One of the two had primary on an external monitor, the other on the
built-in panel. Which one mutter loaded depended on the enumeration race, so
the primary display moved on its own between boots.

The predecessor script (~/.local/bin/fix-monitor-order.py) fixed the ordering
by sorting on EDID serial, which was right, but it also hardcoded
`primary = index 0` — and index 0 was the built-in panel, so it forced primary
back onto the laptop screen every time it ran.

What this does instead
----------------------
Identify monitors by EDID (vendor, product, serial) and never by connector.
Key a stored profile on the *set* of attached monitors plus the serial numbers
of any attached DisplayLink docks, so every dock-and-monitor combination gets
its own profile. Store position, scale, transform, mode, mirroring and — the
part that kept getting lost — which monitor is primary.

On a monitor change:

  * the set of monitors changed  -> restore that combination's profile,
                                    or save the current layout as a new one
  * the set is unchanged         -> the user just rearranged something,
                                    so update the profile to match

so the profile always reflects the last layout that combination had.

Profiles live in ~/.config/dock-monitor-profiles/*.json, one file each, and are
plain readable JSON — `list`, `forget` and a text editor are all valid ways to
manage them.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from gi.repository import Gio, GLib

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"
INTERFACE = BUS_NAME

# ApplyMonitorsConfig methods, from mutter's org.gnome.Mutter.DisplayConfig doc.
METHOD_VERIFY = 0
METHOD_TEMPORARY = 1
METHOD_PERSISTENT = 2

# USB vendor IDs treated as "a dock whose identity matters". DisplayLink
# (17e9) is the only one that applies here: the WAVLINK WL-UG69DK1 exposes
# 17e9:6000 with a real serial (DOCKSERIAL), while the VIA hubs on the same
# cable (2109:2813, 2109:0813) report an empty serial and so cannot identify
# anything. A dock driving monitors over DP alt-mode contributes no USB
# identity at all; such a setup is keyed on its monitors alone, which is still
# distinct as long as the monitors differ.
DOCK_VENDORS = {"17e9"}

USB_DEVICES = Path("/sys/bus/usb/devices")

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "dock-monitor-profiles"

# Seconds to wait for the monitor set to stop changing before acting. Plugging
# the dock in produces a burst of MonitorsChanged as each evdi output binds.
DEFAULT_SETTLE = 3.5

# After applying a profile, ignore auto-save for this long. Our own apply
# causes a MonitorsChanged that would otherwise be read as a user edit.
APPLY_QUIET_PERIOD = 6.0

# How many times to retry the first call at startup, 2s apart.
STARTUP_RETRIES = 15

PROFILE_VERSION = 1


def log(message):
    print(message, flush=True)


def warn(message):
    print(f"warning: {message}", file=sys.stderr, flush=True)


def unpack(value):
    return value.unpack() if isinstance(value, GLib.Variant) else value


# --------------------------------------------------------------------------
# Reading state from mutter
# --------------------------------------------------------------------------


def get_state(bus):
    """Return GetCurrentState as (serial, monitors, logical_monitors, props)."""
    result = bus.call_sync(
        BUS_NAME,
        OBJECT_PATH,
        INTERFACE,
        "GetCurrentState",
        None,
        None,
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )
    return result.unpack()


def monitor_identity(vendor, product, serial):
    """EDID identity of a monitor. Deliberately excludes the connector."""
    return f"{vendor}/{product}/{serial}"


def describe_state(state):
    """Reduce GetCurrentState into the shape everything else works from.

    Returns a dict with:
        by_identity  identity -> {connector, vendor, product, serial, modes,
                                  current_mode, supported_scales}
        logical      list of logical monitors, monitors named by identity
        disabled     identities that are attached but not enabled
        layout_mode  1 = logical (scale-aware), 2 = physical
    """
    _serial, monitors, logical_monitors, props = state

    by_identity = {}
    duplicates = []

    for spec, modes, _monitor_props in monitors:
        connector, vendor, product, serial = spec
        identity = monitor_identity(vendor, product, serial)

        if identity in by_identity:
            # Two monitors that report the same EDID triple cannot be told
            # apart by identity alone. Fall back to including the connector
            # for the colliding pair so behaviour stays deterministic, and say
            # so rather than silently picking one.
            duplicates.append(identity)
            identity = f"{identity}@{connector}"

        current_mode = None
        supported_scales = []
        mode_list = []

        for mode in modes:
            mode_id, width, height, refresh, pref_scale, scales, mode_props = mode
            entry = {
                "id": mode_id,
                "width": int(width),
                "height": int(height),
                "refresh": float(refresh),
                "preferred_scale": float(pref_scale),
                "scales": [float(s) for s in scales],
            }
            mode_list.append(entry)

            if unpack(mode_props.get("is-current", False)):
                current_mode = entry
                supported_scales = entry["scales"]

        by_identity[identity] = {
            "connector": connector,
            "vendor": vendor,
            "product": product,
            "serial": serial,
            "modes": mode_list,
            "current_mode": current_mode,
            "supported_scales": supported_scales,
        }

    for identity in sorted(set(duplicates)):
        warn(
            f"two attached monitors share the EDID identity {identity!r}; "
            "falling back to connector names for those, which are not stable "
            "across hot-plugs"
        )

    identity_by_connector = {
        info["connector"]: identity for identity, info in by_identity.items()
    }

    logical = []
    enabled = set()

    for x, y, scale, transform, primary, specs, _logical_props in logical_monitors:
        members = []

        for connector, _vendor, _product, _serial in specs:
            identity = identity_by_connector.get(connector)
            if identity is None:
                continue
            enabled.add(identity)
            info = by_identity[identity]
            members.append(
                {
                    "identity": identity,
                    "mode": dict(info["current_mode"]) if info["current_mode"] else None,
                }
            )

        if not members:
            continue

        logical.append(
            {
                "x": int(x),
                "y": int(y),
                "scale": float(scale),
                "transform": int(transform),
                "primary": bool(primary),
                "monitors": members,
            }
        )

    disabled = sorted(set(by_identity) - enabled)

    return {
        "by_identity": by_identity,
        "logical": logical,
        "disabled": disabled,
        "layout_mode": int(unpack(props.get("layout-mode", 1))),
    }


# --------------------------------------------------------------------------
# Dock identity and profile keys
# --------------------------------------------------------------------------


def detect_docks():
    """Return sorted 'vendor:product:serial' strings for attached docks.

    Only devices with a non-empty serial are usable: an identity without a
    serial cannot distinguish one dock of a model from another, which is the
    whole point of keying on it.
    """
    found = []

    if not USB_DEVICES.is_dir():
        return found

    for device in USB_DEVICES.iterdir():
        try:
            vendor = (device / "idVendor").read_text().strip()
        except OSError:
            continue

        if vendor not in DOCK_VENDORS:
            continue

        try:
            product = (device / "idProduct").read_text().strip()
            serial = (device / "serial").read_text().strip()
        except OSError:
            continue

        if not serial:
            continue

        found.append(f"{vendor}:{product}:{serial}")

    return sorted(set(found))


def profile_key(described, docks):
    """Stable short key for this dock + monitor combination."""
    lines = [f"dock:{dock}" for dock in docks]
    lines += [f"mon:{identity}" for identity in sorted(described["by_identity"])]
    blob = "\n".join(sorted(lines))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def describe_combination(described, docks):
    """Human-readable label for a profile, for `list` and for the JSON file."""
    monitors = []

    for identity in sorted(described["by_identity"]):
        info = described["by_identity"][identity]
        if info["serial"] and info["serial"] != "0x00000000":
            monitors.append(f"{info['product']} {info['serial']}")
        else:
            monitors.append(info["product"])

    dock_part = ", ".join(docks) if docks else "no dock"
    return f"{dock_part} + " + ", ".join(monitors)


# --------------------------------------------------------------------------
# Profile storage
# --------------------------------------------------------------------------


def profile_path(key):
    return CONFIG_DIR / f"{key}.json"


def build_profile(described, docks, key):
    return {
        "version": PROFILE_VERSION,
        "key": key,
        "description": describe_combination(described, docks),
        "docks": docks,
        "layout_mode": described["layout_mode"],
        "logical_monitors": described["logical"],
        "disabled": described["disabled"],
    }


def layouts_equal(left, right):
    """Compare two profiles ignoring bookkeeping fields and float noise."""

    def normalise(profile):
        logical = []
        for entry in profile.get("logical_monitors", []):
            monitors = sorted(
                (
                    member["identity"],
                    (member.get("mode") or {}).get("id"),
                )
                for member in entry.get("monitors", [])
            )
            logical.append(
                (
                    entry["x"],
                    entry["y"],
                    round(float(entry["scale"]), 6),
                    entry["transform"],
                    bool(entry["primary"]),
                    tuple(monitors),
                )
            )
        return (
            sorted(logical),
            sorted(profile.get("disabled", [])),
            profile.get("layout_mode"),
        )

    return normalise(left) == normalise(right)


def primary_identities(profile):
    return [
        member["identity"]
        for entry in profile.get("logical_monitors", [])
        if entry.get("primary")
        for member in entry.get("monitors", [])
    ]


def carry_primary(current, stored):
    """Keep the stored primary if mutter is currently reporting an ambiguous one.

    mutter's live logical monitors can hold a stale is_primary: applying a
    config that differs *only* in the primary flag does not bump the state
    serial and does not rebuild them, so a wrong flag can sit there until some
    geometry actually changes. This machine was found with two logical
    monitors flagged primary at once for exactly that reason.

    Auto-save must not launder that back into the profile, or the primary
    choice would be lost the first time the user nudged anything.
    """
    if stored is None:
        return current

    current_primaries = primary_identities(current)
    if len(current_primaries) == 1:
        return current

    stored_primaries = primary_identities(stored)
    if len(stored_primaries) != 1:
        return current

    wanted = stored_primaries[0]
    enabled = {
        member["identity"]
        for entry in current.get("logical_monitors", [])
        for member in entry.get("monitors", [])
    }

    if wanted not in enabled:
        return current

    for entry in current["logical_monitors"]:
        entry["primary"] = any(
            member["identity"] == wanted for member in entry["monitors"]
        )

    warn(
        f"mutter reports {len(current_primaries)} primary monitors; keeping "
        f"{wanted} from the stored profile"
    )
    return current


def save_profile(profile, quiet=False):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = profile_path(profile["key"])

    existing = load_profile(profile["key"])
    if existing is not None and layouts_equal(existing, profile):
        if not quiet:
            log(f"profile {profile['key']} already matches, not rewriting")
        return False

    record = dict(profile)
    record["saved"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.replace(path)

    if not quiet:
        verb = "updated" if existing is not None else "saved"
        log(f"{verb} profile {profile['key']}: {profile['description']}")
    return True


def load_profile(key):
    path = profile_path(key)
    if not path.is_file():
        return None

    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        warn(f"could not read {path}: {exc}")
        return None


def all_profiles():
    if not CONFIG_DIR.is_dir():
        return []

    profiles = []
    for path in sorted(CONFIG_DIR.glob("*.json")):
        try:
            profiles.append(json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            warn(f"skipping {path}: {exc}")
    return profiles


# --------------------------------------------------------------------------
# Applying a profile
# --------------------------------------------------------------------------


def pick_mode(info, wanted):
    """Resolve a stored mode against the modes this monitor currently offers.

    Mode ids on mutter 50 are stable strings ('1920x1080@60.000'), so the id
    normally hits. The geometry fallback covers a monitor that re-reports its
    modes slightly differently; the preferred-mode fallback keeps a profile
    usable rather than failing outright, and says when it fired.
    """
    if wanted:
        for mode in info["modes"]:
            if mode["id"] == wanted.get("id"):
                return mode, None

        for mode in info["modes"]:
            if (
                mode["width"] == wanted.get("width")
                and mode["height"] == wanted.get("height")
                and abs(mode["refresh"] - float(wanted.get("refresh", 0))) < 0.05
            ):
                return mode, f"mode {wanted.get('id')} matched by geometry"

    preferred = info["current_mode"] or (info["modes"][0] if info["modes"] else None)
    if preferred is None:
        return None, "monitor reports no modes"

    return preferred, (
        f"mode {wanted.get('id') if wanted else None} unavailable, "
        f"using {preferred['id']}"
    )


def snap_scale(scale, mode, layout_mode):
    """Clamp a stored scale to one the monitor actually supports.

    mutter rejects the whole ApplyMonitorsConfig call if any scale is not in
    the mode's supported list, so a near-miss would lose the entire layout.
    """
    if layout_mode != 1:
        return scale, None

    scales = mode.get("scales") or []
    if not scales:
        return scale, None

    for candidate in scales:
        if abs(candidate - scale) < 1e-6:
            return candidate, None

    nearest = min(scales, key=lambda s: abs(s - scale))
    return nearest, f"scale {scale} unsupported, using {nearest}"


def build_logical_config(described, profile):
    """Turn a stored profile into ApplyMonitorsConfig's logical-monitor array.

    Returns (config, notes). Raises ValueError if the profile cannot be
    applied to the monitors currently attached.
    """
    by_identity = described["by_identity"]
    layout_mode = described["layout_mode"]
    notes = []
    config = []

    for entry in profile.get("logical_monitors", []):
        members = []
        entry_scale = float(entry["scale"])
        scale_source_mode = None

        for member in entry.get("monitors", []):
            identity = member["identity"]
            info = by_identity.get(identity)

            if info is None:
                raise ValueError(f"monitor {identity} is not attached")

            mode, note = pick_mode(info, member.get("mode"))
            if mode is None:
                raise ValueError(f"monitor {identity}: {note}")
            if note:
                notes.append(f"{identity}: {note}")

            if scale_source_mode is None:
                scale_source_mode = mode

            members.append((info["connector"], mode["id"], {}))

        if not members:
            continue

        if scale_source_mode is not None:
            entry_scale, note = snap_scale(entry_scale, scale_source_mode, layout_mode)
            if note:
                notes.append(note)

        config.append(
            (
                int(entry["x"]),
                int(entry["y"]),
                float(entry_scale),
                int(entry["transform"]),
                bool(entry["primary"]),
                members,
            )
        )

    if not config:
        raise ValueError("profile enables no monitors")

    # Exactly one primary. A profile written before this rule, or one whose
    # primary monitor is now disabled, would otherwise leave mutter to pick —
    # and mutter picks the built-in panel, which is the bug being fixed.
    primaries = [i for i, entry in enumerate(config) if entry[4]]

    if len(primaries) != 1:
        keep = primaries[0] if primaries else 0
        if len(primaries) > 1:
            notes.append(
                f"profile marked {len(primaries)} monitors primary, keeping the first"
            )
        else:
            notes.append("profile marked no monitor primary, using the first")
        config = [
            (x, y, scale, transform, i == keep, monitors)
            for i, (x, y, scale, transform, _primary, monitors) in enumerate(config)
        ]

    return config, notes


def apply_profile(bus, profile, method=METHOD_PERSISTENT, dry_run=False):
    """Apply a stored profile. Returns True if a change was pushed to mutter."""
    state = get_state(bus)
    described = describe_state(state)
    current = build_profile(described, detect_docks(), profile["key"])

    if layouts_equal(current, profile):
        log("layout already matches the profile, nothing to do")
        return False

    config, notes = build_logical_config(described, profile)

    for note in notes:
        warn(note)

    if dry_run:
        log("dry run, would apply:")
        for x, y, scale, transform, primary, members in config:
            names = ", ".join(connector for connector, _mode, _props in members)
            flag = " [primary]" if primary else ""
            log(f"  ({x},{y}) scale {scale} transform {transform}: {names}{flag}")
        for identity in profile.get("disabled", []):
            log(f"  disabled: {identity}")
        return False

    parameters = GLib.Variant(
        "(uua(iiduba(ssa{sv}))a{sv})",
        (state[0], method, config, {}),
    )

    bus.call_sync(
        BUS_NAME,
        OBJECT_PATH,
        INTERFACE,
        "ApplyMonitorsConfig",
        parameters,
        None,
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )

    log(f"applied profile {profile['key']}: {profile['description']}")

    for x, y, scale, _transform, primary, members in config:
        names = ", ".join(connector for connector, _mode, _props in members)
        flag = " [primary]" if primary else ""
        log(f"  ({x},{y}) scale {scale}: {names}{flag}")

    return True


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def snapshot(bus):
    state = get_state(bus)
    described = describe_state(state)
    docks = detect_docks()
    key = profile_key(described, docks)
    return described, docks, key


def cmd_show(bus, args):
    described, docks, key = snapshot(bus)
    profile = load_profile(key)

    log(f"profile key:  {key}")
    log(f"docks:        {', '.join(docks) if docks else 'none detected'}")
    log(f"layout mode:  {'logical' if described['layout_mode'] == 1 else 'physical'}")
    log(f"stored:       {'yes, ' + str(profile_path(key)) if profile else 'no'}")
    log("")
    log("attached monitors:")

    for identity in sorted(described["by_identity"]):
        info = described["by_identity"][identity]
        mode = info["current_mode"]
        mode_id = mode["id"] if mode else "off"
        log(f"  {identity}")
        log(f"      on {info['connector']}, mode {mode_id}")

    log("")
    log("current layout:")

    for entry in described["logical"]:
        names = ", ".join(member["identity"] for member in entry["monitors"])
        flag = " [primary]" if entry["primary"] else ""
        log(
            f"  ({entry['x']},{entry['y']}) scale {entry['scale']} "
            f"transform {entry['transform']}: {names}{flag}"
        )

    for identity in described["disabled"]:
        log(f"  disabled: {identity}")

    if profile and not layouts_equal(
        build_profile(described, docks, key), profile
    ):
        log("")
        log("current layout differs from the stored profile")

    return 0


def set_primary(profile, pattern):
    """Force one logical monitor primary, matched by substring on identity."""
    needle = pattern.casefold()
    matches = [
        index
        for index, entry in enumerate(profile["logical_monitors"])
        if any(
            needle in member["identity"].casefold()
            for member in entry.get("monitors", [])
        )
    ]

    if not matches:
        raise ValueError(f"no enabled monitor matches {pattern!r}")

    if len(matches) > 1:
        names = [
            ", ".join(m["identity"] for m in profile["logical_monitors"][i]["monitors"])
            for i in matches
        ]
        raise ValueError(
            f"{pattern!r} matches more than one monitor: " + "; ".join(names)
        )

    for index, entry in enumerate(profile["logical_monitors"]):
        entry["primary"] = index == matches[0]


def cmd_save(bus, args):
    described, docks, key = snapshot(bus)
    profile = build_profile(described, docks, key)

    if args.primary:
        try:
            set_primary(profile, args.primary)
        except ValueError as exc:
            warn(str(exc))
            return 1
    else:
        # mutter can report a layout with no primary, or — as seen on this
        # machine while two competing scripts fought over it — with more than
        # one. Storing that would carry the ambiguity forward, so say so.
        count = sum(1 for e in profile["logical_monitors"] if e["primary"])
        if count != 1:
            warn(
                f"mutter reports {count} primary monitors right now; the "
                "profile will record that as-is. Use --primary to state "
                "which one you want."
            )

    if args.description:
        profile["description"] = args.description

    if args.dry_run:
        log(f"dry run, would save profile {key}:")
        log(json.dumps(profile, indent=2))
        return 0

    save_profile(profile)
    return 0


def cmd_apply(bus, args):
    described, docks, key = snapshot(bus)
    profile = load_profile(key)

    if profile is None:
        log(f"no profile stored for {key} ({describe_combination(described, docks)})")
        log("run 'dock-monitor-profiles save' to create one")
        return 1

    method = METHOD_TEMPORARY if args.temporary else METHOD_PERSISTENT

    try:
        apply_profile(bus, profile, method=method, dry_run=args.dry_run)
    except (ValueError, GLib.Error) as exc:
        warn(f"could not apply profile {key}: {exc}")
        return 1

    return 0


def cmd_list(bus, args):
    profiles = all_profiles()

    if not profiles:
        log(f"no profiles stored in {CONFIG_DIR}")
        return 0

    try:
        _described, _docks, current_key = snapshot(bus)
    except GLib.Error:
        current_key = None

    for profile in profiles:
        marker = " *" if profile.get("key") == current_key else "  "
        log(f"{marker} {profile.get('key')}  {profile.get('description', '')}")
        log(f"     saved {profile.get('saved', 'unknown')}")

        for entry in profile.get("logical_monitors", []):
            names = ", ".join(
                member["identity"] for member in entry.get("monitors", [])
            )
            flag = " [primary]" if entry.get("primary") else ""
            log(f"     ({entry['x']},{entry['y']}) scale {entry['scale']}: {names}{flag}")

        for identity in profile.get("disabled", []):
            log(f"     disabled: {identity}")

        log("")

    if current_key:
        log("* = the combination attached now")

    return 0


def cmd_forget(bus, args):
    if args.key:
        key = args.key
    else:
        _described, _docks, key = snapshot(bus)

    path = profile_path(key)

    if not path.is_file():
        log(f"no profile stored for {key}")
        return 1

    path.unlink()
    log(f"removed {path}")
    return 0


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------


class Daemon:
    """Watch MonitorsChanged and keep the layout in sync with the profiles."""

    def __init__(self, bus, settle, method, dry_run=False):
        self.bus = bus
        self.settle = settle
        self.method = method
        self.dry_run = dry_run
        self.pending = None
        self.last_key = None
        self.quiet_until = 0.0
        # At login this unit can start before mutter has taken the
        # org.gnome.Mutter.DisplayConfig name, and no MonitorsChanged will
        # arrive to prompt a retry. Keep retrying until the first call lands.
        self.ready = False
        self.startup_tries = 0

    def start(self):
        self.bus.signal_subscribe(
            BUS_NAME,
            INTERFACE,
            "MonitorsChanged",
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self.on_monitors_changed,
            None,
        )

        # Settle once at startup too: at login the session has just come up
        # and nothing has fired yet.
        self.schedule()

        log(f"watching for monitor changes; profiles in {CONFIG_DIR}")
        GLib.MainLoop().run()

    def on_monitors_changed(self, *_args):
        self.schedule()

    def schedule(self):
        if self.pending is not None:
            GLib.source_remove(self.pending)

        self.pending = GLib.timeout_add(
            int(self.settle * 1000), self.settled
        )

    def settled(self):
        self.pending = None

        try:
            self.reconcile()
            self.ready = True
        except GLib.Error as exc:
            if not self.ready and self.startup_tries < STARTUP_RETRIES:
                self.startup_tries += 1
                log(
                    f"mutter not answering yet ({self.startup_tries}"
                    f"/{STARTUP_RETRIES}), retrying"
                )
                self.pending = GLib.timeout_add_seconds(2, self.settled)
                return GLib.SOURCE_REMOVE
            warn(f"mutter call failed: {exc}")
        except Exception as exc:  # keep the daemon alive
            warn(f"unexpected error: {exc}")

        return GLib.SOURCE_REMOVE

    def reconcile(self):
        described, docks, key = snapshot(self.bus)
        current = build_profile(described, docks, key)
        stored = load_profile(key)

        combination_changed = key != self.last_key
        self.last_key = key

        if combination_changed:
            log(f"combination {key}: {current['description']}")

            if stored is None:
                log("no profile for this combination yet, saving the current layout")
                save_profile(current)
                return

            try:
                changed = apply_profile(
                    self.bus, stored, method=self.method, dry_run=self.dry_run
                )
            except (ValueError, GLib.Error) as exc:
                warn(f"could not apply profile {key}: {exc}")
                warn("keeping the current layout and saving it instead")
                save_profile(current)
                return

            if changed:
                self.quiet_until = time.monotonic() + APPLY_QUIET_PERIOD
            return

        # Same monitors as last time, so a change here is the user rearranging
        # things in Settings. Record it — unless we caused it ourselves.
        if time.monotonic() < self.quiet_until:
            return

        current = carry_primary(current, stored)

        if stored is not None and layouts_equal(stored, current):
            return

        if self.dry_run:
            log(f"dry run, would update profile {key}")
            return

        save_profile(current)


# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Remember and restore GNOME monitor layouts per dock and monitor "
            "combination, identifying monitors by EDID rather than by "
            "connector name."
        )
    )
    # Flags that change what gets written go on the subcommands that can
    # write, so that `save --dry-run` reads the way it looks.
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen without changing anything",
    )

    temp = argparse.ArgumentParser(add_help=False)
    temp.add_argument(
        "--temporary",
        action="store_true",
        help=(
            "apply without asking mutter to persist to monitors.xml "
            "(default is persistent, so the layout survives even if this "
            "tool is not running)"
        ),
    )

    sub = parser.add_subparsers(dest="command")

    p_show = sub.add_parser("show", help="show the current layout and profile key")
    p_show.set_defaults(func=cmd_show)

    p_save = sub.add_parser(
        "save",
        parents=[dry],
        help="save the current layout as this combination's profile",
    )
    p_save.add_argument("--description", help="override the generated description")
    p_save.add_argument(
        "--primary",
        help=(
            "record this monitor as primary, matched as a substring of its "
            "identity (e.g. a serial number); overrides what mutter reports"
        ),
    )
    p_save.set_defaults(func=cmd_save)

    p_apply = sub.add_parser(
        "apply",
        parents=[dry, temp],
        help="apply this combination's stored profile now",
    )
    p_apply.set_defaults(func=cmd_apply)

    p_list = sub.add_parser("list", help="list stored profiles")
    p_list.set_defaults(func=cmd_list)

    p_forget = sub.add_parser("forget", help="delete a stored profile")
    p_forget.add_argument(
        "key", nargs="?", help="profile key; defaults to the combination attached now"
    )
    p_forget.set_defaults(func=cmd_forget)

    p_daemon = sub.add_parser(
        "daemon",
        parents=[dry, temp],
        help="watch for monitor changes and act",
    )
    p_daemon.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE,
        help=(
            "seconds to wait for the monitor set to stop changing "
            f"(default {DEFAULT_SETTLE})"
        ),
    )
    p_daemon.set_defaults(func=None)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 2

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        warn(f"no session bus: {exc}")
        return 1

    if args.command == "daemon":
        method = METHOD_TEMPORARY if args.temporary else METHOD_PERSISTENT
        Daemon(bus, args.settle, method, dry_run=args.dry_run).start()
        return 0

    try:
        return args.func(bus, args)
    except GLib.Error as exc:
        warn(f"mutter call failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
