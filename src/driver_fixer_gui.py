#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WingFlight Driver Fixer
========================
A small GUI tool that gets a flight controller's USB DFU/bootloader driver
installed on Windows, so a browser-based configurator (WebUSB) can see it.

One button does the whole job (mirrors ImpulseRC's Driver Fixer): find the
flight controller, identify it, reboot it to bootloader/DFU if it isn't
already there, then install the matching WinUSB driver via pnputil. The end
goal is always "driver installed" — rebooting is just a means to that end.
"""

import sys
import json
import time
import threading
import subprocess
import webbrowser
from collections import namedtuple
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox
except ImportError:
    print("Error: tkinter is required but not found.")
    sys.exit(1)

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("Error: pyserial is required. Install with: pip install pyserial")
    sys.exit(1)


def _get_resource_dir():
    # PyInstaller --onefile unpacks bundled --add-data files to a temp
    # _MEIPASS directory at runtime, not next to the exe.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


RESOURCE_DIR = _get_resource_dir()
DRIVERS_DIR = RESOURCE_DIR / "drivers"

GITHUB_REPO_URL = "https://github.com/WingFlight/wingflight-driver-fixer"
ZADIG_URL = "https://zadig.akeo.ie/"

# USB IDs the wingflight-configurator itself watches for in DFU mode
# (src/js/port_handler.js: usbDevices.filters).
STM32_DFU = (0x0483, 0xDF11)
GD32_DFU = (0x28E9, 0x0189)

# MSP_SET_REBOOT / REBOOT_TYPES.BOOTLOADER, shared across the
# betaflight/inav/wingflight MSP lineage (src/js/msp/MSPCodes.js,
# src/js/msp/MSPHelper.js in wingflight-configurator).
MSP_API_VERSION = 1
MSP_SET_REBOOT = 68
REBOOT_BOOTLOADER = 1
REBOOT_BAUD = 115200
REBOOT_WAIT_SECONDS = 8.0
REBOOT_POLL_SECONDS = 1.0
IDENTIFY_TIMEOUT_SECONDS = 0.4

# Friendly hints for common flight-controller USB-serial chips. Not required
# for the app to function, just makes the device list easier to read.
KNOWN_SERIAL_CHIPS = {
    (0x0483, 0x5740): "STM32 Virtual COM Port",
    (0x10C4, 0xEA60): "CP210x USB-UART",
    (0x1A86, 0x7523): "CH340 USB-UART",
    (0x0403, 0x6001): "FTDI FT232",
}

DRIVER_PACKAGES = {
    STM32_DFU: {
        "label": "STM32 DFU (signed)",
        "inf": DRIVERS_DIR / "stm32" / "STM32Bootloader.inf",
    },
    GD32_DFU: {
        "label": "GD32/AT32 DFU (unsigned, best-effort)",
        "inf": DRIVERS_DIR / "gd32" / "GD32Bootloader.inf",
    },
}


def is_admin():
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def build_msp_v1(code, payload=b""):
    """Build an MSP v1 request packet: $M< + len + code + payload + checksum."""
    body = bytes([len(payload), code]) + payload
    checksum = 0
    for b in body:
        checksum ^= b
    return b"$M<" + body + bytes([checksum])


def reboot_to_bootloader(port_name):
    packet = build_msp_v1(MSP_SET_REBOOT, bytes([REBOOT_BOOTLOADER]))
    with serial.Serial(port_name, REBOOT_BAUD, timeout=2.0) as ser:
        ser.write(packet)
        ser.flush()


def probe_msp(port_name, timeout=IDENTIFY_TIMEOUT_SECONDS):
    """Ask a serial port to identify itself over MSP (MSP_API_VERSION, the
    lightest-weight query, supported the same way across the betaflight/
    inav/wingflight MSP lineage). Returns True only if something answers
    with a well-formed MSP response frame — i.e. this is actually a flight
    controller, not just some unrelated serial device.
    """
    try:
        with serial.Serial(port_name, REBOOT_BAUD, timeout=timeout) as ser:
            ser.reset_input_buffer()
            ser.write(build_msp_v1(MSP_API_VERSION))
            ser.flush()
            response = ser.read(32)
    except Exception:
        return False
    return b"$M>" in response or b"$M!" in response


# Prevent every subprocess call (powershell.exe, pnputil.exe) from flashing
# its own console window — this app has no console of its own (--windowed
# build / pythonw), so without this each call pops a visible window.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_hidden(cmd, timeout):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def _hwid_fragment(vid, pid):
    return f"VID_{vid:04X}&PID_{pid:04X}"


def scan_dfu_devices():
    """Query Windows PnP entries directly for known DFU USB IDs, regardless
    of whether a driver (and therefore a COM port) is bound to them yet.

    A device sitting in bootloader/DFU mode with no driver installed will
    NEVER show up via pyserial's list_ports (which only enumerates devices
    Windows has already assigned a COM port to) — that's the whole reason
    this app exists, so detection has to go around that layer.
    Returns a list of (dfu_key, instance_id, friendly_name, driver_ok), where
    driver_ok reflects PnP Status: 'OK' once our WinUSB driver is bound,
    something else (typically 'Error'/'Unknown') while the device is still
    driverless.
    """
    if sys.platform != "win32":
        return []

    fragments = [_hwid_fragment(vid, pid) for vid, pid in DRIVER_PACKAGES]
    regex = "|".join(fragments)
    ps_command = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"Get-PnpDevice -PresentOnly | Where-Object {{ $_.InstanceId -match '{regex}' }} "
        "| Select-Object InstanceId, FriendlyName, Status | ConvertTo-Json -Compress"
    )
    try:
        result = _run_hidden(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_command],
            timeout=10,
        )
    except Exception:
        return []

    text = (result.stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    found = []
    for entry in data:
        instance_id = str(entry.get("InstanceId", "")).upper()
        friendly = entry.get("FriendlyName") or "USB Device"
        driver_ok = str(entry.get("Status", "")).strip().upper() == "OK"
        for vid, pid in DRIVER_PACKAGES:
            if _hwid_fragment(vid, pid) in instance_id:
                found.append(((vid, pid), instance_id, friendly, driver_ok))
                break
    return found


DeviceRow = namedtuple("DeviceRow", ["id", "label", "vidpid_str", "dfu_key", "is_serial", "driver_ok"])


def describe_port(port):
    if port.vid is not None and port.pid is not None:
        vid_pid = (port.vid, port.pid)
        if vid_pid in DRIVER_PACKAGES:
            return DRIVER_PACKAGES[vid_pid]["label"]
        if vid_pid in KNOWN_SERIAL_CHIPS:
            return KNOWN_SERIAL_CHIPS[vid_pid]
    return port.description or "Unknown device"


def scan_candidate_devices():
    """Enumerate every serial port plus any raw USB DFU device found via
    Windows PnP — unfiltered. Serial ports here are only *candidates*; most
    of them (a machine's onboard COM1, a Bluetooth SPP port, etc.) are not
    flight controllers and still need to be identified via MSP before
    they're shown to the user."""
    rows = []
    dfu_keys_seen = set()

    for port in list_ports.comports():
        vid_pid = (port.vid, port.pid) if port.vid is not None else None
        dfu_key = vid_pid if vid_pid in DRIVER_PACKAGES else None
        if dfu_key:
            dfu_keys_seen.add(dfu_key)
        vidpid_str = f"{port.vid:04X}:{port.pid:04X}" if port.vid is not None else "-"
        rows.append(DeviceRow(
            id=port.device, label=describe_port(port),
            vidpid_str=vidpid_str, dfu_key=dfu_key, is_serial=True,
            # A COM port already existing means the OS has some driver
            # bound to it, whatever it is.
            driver_ok=True,
        ))

    for dfu_key, instance_id, friendly, driver_ok in scan_dfu_devices():
        if dfu_key in dfu_keys_seen:
            continue  # already listed via its COM port above
        vid, pid = dfu_key
        rows.append(DeviceRow(
            id=instance_id, label=f"{friendly} — {DRIVER_PACKAGES[dfu_key]['label']}",
            vidpid_str=f"{vid:04X}:{pid:04X}", dfu_key=dfu_key, is_serial=False,
            driver_ok=driver_ok,
        ))

    return rows


def scan_identified_devices():
    """The list this app actually shows/acts on: devices already in DFU
    mode (no identification needed — the USB ID says what they are) plus
    serial ports that positively answered an MSP query. Anything that
    doesn't respond to MSP is assumed to be unrelated hardware and is left
    out entirely, rather than shown as an unusable "maybe" entry.

    Blocking (probes each candidate serial port) — call from a worker
    thread, never the UI thread.
    """
    candidates = scan_candidate_devices()
    identified = [row for row in candidates if row.dfu_key]
    for row in candidates:
        if not row.dfu_key and probe_msp(row.id):
            identified.append(row)
    return identified


def install_driver(inf_path):
    """Run pnputil /add-driver for the given INF. Returns (ok, message).

    pnputil stages the driver into the driver store and installs it onto
    every currently-present device matching the INF's hardware ID, so this
    doesn't need to target a specific device instance.
    """
    if not inf_path.is_file():
        return False, f"Driver file not found: {inf_path}"
    try:
        result = _run_hidden(
            ["pnputil.exe", "/add-driver", str(inf_path), "/install"],
            timeout=30,
        )
    except Exception as e:
        return False, f"Failed to run pnputil: {e}"

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return True, output.strip() or "Driver installed."
    return False, output.strip() or f"pnputil exited with code {result.returncode}"


class DriverFixerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("WingFlight Driver Fixer")
        self.root.geometry("760x600")
        self.root.resizable(False, False)

        self.devices = []  # list of DeviceRow
        self.busy = False

        self.setup_ui()
        self.refresh_devices()

    # -- UI construction -------------------------------------------------

    def setup_ui(self):
        header_bg = "#1f1f1f"
        header_fg = "#f2f2f2"

        header = tk.Frame(self.root, bg=header_bg, height=80)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        text_frame = tk.Frame(header, bg=header_bg)
        text_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12, pady=10)

        tk.Label(
            text_frame, text="WingFlight Driver Fixer",
            font=("Arial", 16, "bold"), bg=header_bg, fg=header_fg,
        ).pack(anchor=tk.W)
        tk.Label(
            text_frame, text="Plug in your flight controller, click Fix Driver — that's it",
            font=("Arial", 10), bg=header_bg, fg=header_fg,
        ).pack(anchor=tk.W, pady=(2, 0))

        logo_path = RESOURCE_DIR / "logo.png"
        if logo_path.is_file():
            try:
                self.logo_image = tk.PhotoImage(file=str(logo_path))
                logo_frame = tk.Frame(header, bg=header_bg, width=160, height=60)
                logo_frame.pack(side=tk.RIGHT, padx=12, pady=10)
                logo_frame.pack_propagate(False)
                tk.Label(logo_frame, image=self.logo_image, bg=header_bg).pack(anchor=tk.E)
            except Exception:
                self.logo_image = None

        if not is_admin():
            warn = tk.Frame(self.root, bg="#5a3d00")
            warn.pack(fill=tk.X)
            tk.Label(
                warn,
                text="Not running as Administrator — driver installation will fail. Re-launch as admin.",
                bg="#5a3d00", fg="#ffd479", font=("Arial", 9, "bold"), pady=4,
            ).pack()

        body = ttk.Frame(self.root, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        columns = ("port", "description", "vidpid", "status")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", height=7, selectmode="browse")
        for col, text, width in (
            ("port", "Port", 90),
            ("description", "Description", 300),
            ("vidpid", "VID:PID", 90),
            ("status", "Status", 220),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=tk.W)
        self.tree.pack(fill=tk.X)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.update_button_states())

        # Primary action: one big, unmissable button that does the whole job.
        primary_frame = ttk.Frame(self.root, padding=(10, 12, 10, 4))
        primary_frame.pack(fill=tk.X)
        fix_style = ttk.Style()
        fix_style.configure("Fix.TButton", font=("Arial", 13, "bold"), padding=10)
        self.fix_button = ttk.Button(
            primary_frame, text="Fix Driver", style="Fix.TButton",
            command=self.on_fix_click, width=24,
        )
        self.fix_button.pack()

        # Secondary/utility actions.
        button_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        button_frame.pack(fill=tk.X)
        self.refresh_button = ttk.Button(button_frame, text="Refresh", command=self.refresh_devices)
        self.refresh_button.pack(side=tk.LEFT)

        log_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=13, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        footer = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        footer.pack(fill=tk.X)
        ttk.Button(
            footer, text="View on GitHub", command=lambda: webbrowser.open(GITHUB_REPO_URL),
        ).pack(side=tk.RIGHT)

    # -- logging -----------------------------------------------------------

    def log(self, message):
        def append():
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.root.after(0, append)

    # -- device list ---------------------------------------------------

    def refresh_devices(self):
        """Rescan in the background and repopulate the list once done.
        Only devices already in DFU mode, or serial ports confirmed to be a
        flight controller via MSP, are ever shown. Owns the busy flag for
        the duration of its own scan — don't call this while another
        operation (e.g. the fix pipeline) is already in flight and expects
        to keep holding "busy" itself."""
        previous = self.tree.selection()[0] if self.tree.selection() else None
        self.set_busy(True)
        self.log("Scanning and identifying connected devices...")

        def worker():
            devices = scan_identified_devices()
            def apply():
                self._apply_scan_result(devices, previous)
                self.set_busy(False)
            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _status_for(row):
        if row.dfu_key:
            return "Driver installed — ready for the configurator" if row.driver_ok else "In DFU mode — click Fix Driver to install its driver"
        return "Flight controller (MSP confirmed) — click Fix Driver to reboot + install"

    def _apply_scan_result(self, devices, previous):
        """Pure UI update — populate the tree from a completed scan. Does
        NOT touch the busy flag, since this is also used as a mid-pipeline
        progress update while 'Fix Driver' is still busy doing its job."""
        self.devices = devices
        self.tree.delete(*self.tree.get_children())
        for row in devices:
            status = self._status_for(row)
            self.tree.insert(
                "", tk.END, iid=row.id,
                values=(row.id if row.is_serial else "(DFU)", row.label, row.vidpid_str, status),
            )

        if previous and self.tree.exists(previous):
            self.tree.selection_set(previous)
        elif len(devices) == 1:
            # Only one candidate connected — auto-select it so "Fix Driver"
            # is a true single click in the common case.
            self.tree.selection_set(devices[0].id)

        self.log(f"Scan complete: {len(devices)} flight controller device(s) found.")

    def update_button_states(self):
        self.fix_button.configure(state=tk.DISABLED if self.busy else tk.NORMAL)

    def set_busy(self, busy):
        self.busy = busy
        self.refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.update_button_states()

    # -- actions ---------------------------------------------------------

    @staticmethod
    def _pick_unambiguous(devices):
        if len(devices) == 1:
            return devices[0]
        dfu_candidates = [d for d in devices if d.dfu_key]
        if len(dfu_candidates) == 1:
            return dfu_candidates[0]
        return None

    def on_fix_click(self):
        # Capture the user's current selection here, on the UI thread —
        # Tkinter widgets must not be touched from a worker thread.
        selection = self.tree.selection()
        selected_id = selection[0] if selection else None

        self.set_busy(True)
        threading.Thread(target=self._fix_pipeline_worker, args=(selected_id,), daemon=True).start()

    def _fix_pipeline_worker(self, selected_id):
        try:
            self.log("Finding and identifying your flight controller...")
            devices = scan_identified_devices()
            self.root.after(0, lambda: self._apply_scan_result(devices, selected_id))

            row = None
            if selected_id:
                row = next((d for d in devices if d.id == selected_id), None)
            if not row:
                row = self._pick_unambiguous(devices)

            if not row:
                if not devices:
                    self.log("No flight controller found. Plug it in (in normal mode or already in DFU) "
                             "and click Fix Driver again.")
                else:
                    self.log("Multiple flight controllers found — select the one to fix in the list above, "
                             "then click Fix Driver again.")
                return

            self._run_fix(row)
        finally:
            # A single rescan at the end both refreshes the visible list and
            # re-enables the Fix Driver button (refresh_devices owns the
            # busy flag for the duration of its own background scan).
            self.root.after(0, self.refresh_devices)

    def _run_fix(self, row):
        try:
            self.log(f"— Fixing driver for {row.label} ({row.id}) —")
            dfu_key = row.dfu_key

            if not dfu_key:
                self.log(f"Step 1/3: Rebooting {row.id} to bootloader...")
                reboot_to_bootloader(row.id)
                self.log("Step 2/3: Waiting for device to re-enumerate in DFU mode...")

                deadline = time.time() + REBOOT_WAIT_SECONDS
                while time.time() < deadline and not dfu_key:
                    time.sleep(REBOOT_POLL_SECONDS)
                    for key, _instance_id, _friendly, _driver_ok in scan_dfu_devices():
                        dfu_key = key
                        break

                if not dfu_key:
                    self.log("⚠ Did not see a DFU device appear. If the board didn't reboot, put it in "
                             "bootloader mode manually (boot button) and click Fix Driver again.")
                    return
            else:
                self.log("Steps 1-2/3: Device is already in DFU mode — no reboot needed.")

            package = DRIVER_PACKAGES[dfu_key]
            self.log(f"Step 3/3: Installing {package['label']} driver...")
            ok, message = install_driver(package["inf"])

            if ok:
                self.log(f"✓ Driver install: {message}")
                self.log("✓ Done. The device should now be visible to the configurator.")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Driver Installed", f"{package['label']} driver installed successfully."))
            else:
                self.log(f"✗ Driver install failed: {message}")
                hint = ""
                if dfu_key == GD32_DFU:
                    hint = ("\n\nThis driver is unsigned; Windows may be blocking it due to "
                            f"driver-signature enforcement. As a manual fallback, try Zadig: {ZADIG_URL}")
                self.root.after(0, lambda: messagebox.showerror(
                    "Driver Install Failed", f"{message}{hint}"))
        except Exception as e:
            self.log(f"✗ Failed: {e}")


def main():
    root = tk.Tk()
    icon_path = RESOURCE_DIR / "icon.ico"
    if sys.platform == "win32" and icon_path.is_file():
        try:
            root.iconbitmap(str(icon_path))
        except Exception:
            pass
    DriverFixerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
