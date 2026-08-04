#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dev/test helper: strip every staged driver package currently associated
with the DFU bootloader hardware IDs WingFlight Driver Fixer targets, so a
board can be put back into a driverless state to re-test "Fix Driver" from
scratch. Not part of the shipped app.

This removes both WingFlight Driver Fixer's own package (STM32Bootloader.inf
/ GD32Bootloader.inf) AND whatever other package is actually bound right
now — e.g. a leftover driver from STM32CubeProgrammer, ST-Link Utility, or
(commonly) a previous ImpulseRC Driver Fixer run, which stages its own
WinUSB package for the same VID_0483&PID_DF11 ID under a different name
("ImpulseRC Flight Controller"). Removing only our own package is not
enough to get a clean test if one of those is what's actually winning.

Must be run elevated (same requirement as installing).
"""

import json
import re
import subprocess
import sys

TARGET_ORIGINAL_NAMES = {"stm32bootloader.inf", "gd32bootloader.inf"}
DFU_HWID_PATTERN = "VID_0483&PID_DF11|VID_28E9&PID_0189"


def is_admin():
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def enum_driver_packages():
    """Parse `pnputil /enum-drivers` into a list of {field: value} dicts,
    one per driver package block (blocks are blank-line separated)."""
    result = subprocess.run(
        ["pnputil.exe", "/enum-drivers"], capture_output=True, text=True, timeout=30,
    )
    packages = []
    current = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                packages.append(current)
                current = {}
            continue
        match = re.match(r"([^:]+):\s*(.*)", line)
        if match:
            current[match.group(1).strip()] = match.group(2).strip()
    if current:
        packages.append(current)
    return packages


def bound_driver_info():
    """Ground truth: for every present device matching our DFU hardware
    IDs, what .inf/provider/version is actually bound right now — via
    DEVPKEY_Device_DriverInfPath, not by name-matching a package we expect.
    This tells us definitively whether some other driver package (already
    on the machine from STM32CubeProgrammer, ST-Link Utility, Zadig, an
    automatic Microsoft OS Descriptor WinUSB bind, etc.) is what's actually
    serving the device — independent of what we did or didn't uninstall.
    """
    ps_command = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"$devices = Get-PnpDevice -PresentOnly | Where-Object {{ $_.InstanceId -match '{DFU_HWID_PATTERN}' }}; "
        "$out = foreach ($d in $devices) { "
        "  $inf = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverInfPath').Data; "
        "  $prov = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverProvider').Data; "
        "  $ver = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverVersion').Data; "
        "  [PSCustomObject]@{ InstanceId=$d.InstanceId; Status=$d.Status; DriverInf=$inf; DriverProvider=$prov; DriverVersion=$ver } "
        "}; $out | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_command],
        capture_output=True, text=True, timeout=15,
    )
    text = (result.stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    return [data] if isinstance(data, dict) else data


def print_bound_driver_info(label, packages_by_published_name):
    print(f"--- Bound driver, {label} ---")
    devices = bound_driver_info()
    if not devices:
        print("  No DFU-mode device currently present. Put the board in DFU mode, then re-run this script.")
        return
    for d in devices:
        inf = d.get("DriverInf") or None
        print(f"  {d.get('InstanceId')}")
        print(f"    Status: {d.get('Status')}   Bound .inf: {inf or '(none)'}")
        if not inf:
            continue

        pkg = packages_by_published_name.get(str(inf).lower())
        if pkg:
            original = pkg.get("Original Name", "?")
            provider = pkg.get("Provider Name", "?")
            print(f"    -> Staged package: original name '{original}', provider '{provider}'")
            if original.strip().lower() not in TARGET_ORIGINAL_NAMES:
                print("    -> This is NOT a WingFlight Driver Fixer package. Something else on this "
                      "machine (STM32CubeProgrammer, ST-Link Utility, a prior Zadig run, etc.) staged it.")
        elif not str(inf).lower().startswith("oem"):
            print(f"    -> '{inf}' is one of Windows' inbox drivers, not a staged oem package. This usually "
                  "means the device was auto-bound via its own Microsoft OS Descriptor (WinUSB compatible ID) "
                  "rather than any driver we — or anyone else — explicitly installed, so uninstalling our "
                  "package has no effect on it.")
        else:
            print(f"    -> '{inf}' isn't in the current driver-store listing (already removed, or stale).")
    print()


def main():
    if not is_admin():
        print("ERROR: this must be run from an elevated (Administrator) prompt.")
        print("Use uninstall_driver.cmd, which requests elevation automatically.")
        return 1

    print("Enumerating driver packages in the Windows driver store...")
    packages = enum_driver_packages()
    packages_by_published_name = {
        pkg["Published Name"].lower(): pkg for pkg in packages if pkg.get("Published Name")
    }

    print_bound_driver_info("BEFORE removal", packages_by_published_name)

    # Anything currently bound to our DFU hardware IDs (regardless of who
    # published it) — this is the package Windows is actually using.
    bound_published = {
        str(d.get("DriverInf")).lower()
        for d in bound_driver_info()
        if d.get("DriverInf") and str(d.get("DriverInf")).lower().startswith("oem")
    }
    # Plus our own known packages, in case they're staged but not currently
    # the winning match for a present device.
    own_published = {
        pkg["Published Name"].lower()
        for pkg in packages
        if pkg.get("Original Name", "").strip().lower() in TARGET_ORIGINAL_NAMES and pkg.get("Published Name")
    }
    to_remove = bound_published | own_published

    if not to_remove:
        print("Nothing staged to remove. If the device is still shown as working, it's likely auto-bound "
              "via an inbox driver (e.g. winusb.inf) that's part of Windows itself and can't be removed.")
    else:
        for published in sorted(to_remove):
            pkg = packages_by_published_name.get(published)
            original = pkg.get("Original Name", "?") if pkg else "?"
            provider = pkg.get("Provider Name", "?") if pkg else "?"
            print(f"Removing {published} (original: {original}, provider: {provider})...")
            result = subprocess.run(
                ["pnputil.exe", "/delete-driver", published, "/uninstall", "/force"],
                capture_output=True, text=True, timeout=30,
            )
            for line in (result.stdout or "").strip().splitlines():
                print(f"  {line}")
            if result.returncode != 0:
                print(f"  -> pnputil exited with code {result.returncode}: {(result.stderr or '').strip()}")

    print()
    input("Unplug/replug the board (or re-enter DFU mode) now, then press Enter to check what's bound afterwards...")

    packages_after = enum_driver_packages()
    packages_by_published_name_after = {
        pkg["Published Name"].lower(): pkg for pkg in packages_after if pkg.get("Published Name")
    }
    print_bound_driver_info("AFTER uninstall", packages_by_published_name_after)

    return 0


if __name__ == "__main__":
    sys.exit(main())
