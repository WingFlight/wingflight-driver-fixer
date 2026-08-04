# WingFlight Driver Fixer

A small Windows tool that gets a flight controller's USB bootloader/DFU
driver installed, so a browser-based configurator (WebUSB) can see it.

The Wingflight Configurator is moving to a web app, and browsers cannot
install Windows drivers themselves. If a flight controller ends up in
bootloader/DFU mode without the right WinUSB driver installed, this tool
fixes that — similar in spirit to ImpulseRC's Driver Fixer.

## What it does

One button — **Fix Driver** — does the whole job:

1. **Find** — scans serial ports for a connected flight controller (or a
   device already sitting in DFU/bootloader mode). If exactly one candidate
   is connected, it's auto-selected; you only need to pick from the list
   yourself if multiple devices are plugged in.
2. **Identify** — classifies the selected device: already in DFU mode, or
   a normal flight controller that needs rebooting first.
3. **Reboot** — if needed, sends the standard MSP reboot-to-bootloader
   command over its serial port (the same command the Wingflight
   Configurator itself uses, shared across the betaflight/inav/wingflight
   MSP lineage), then waits for it to re-enumerate in DFU mode.
4. **Install** — installs the matching WinUSB driver via `pnputil`, the
   same mechanism the Wingflight Configurator's Windows installer already
   uses.

Rebooting is just a means to that end — the goal the app is built around is
always "driver installed," not "device rebooted."

Supported bootloader USB IDs (matching what `wingflight-configurator`
itself watches for):

| Target       | USB ID        | Driver status                              |
|--------------|---------------|---------------------------------------------|
| STM32 DFU    | `0483:DF11`   | Signed driver package, fully supported       |
| GD32/AT32 DFU| `28E9:0189`   | Best-effort — unsigned, see note below       |

### A note on the GD32/AT32 driver

There is no vendor-supplied, Microsoft-signed driver catalog for the
GD32/AT32 DFU bootloader ID, so `drivers/gd32/GD32Bootloader.inf` was
authored from scratch using the standard Microsoft WinUSB co-installer INF
template (same structure as the STM32 package, just retargeted). Windows'
driver-signature-enforcement policy may block or warn on installing it,
depending on the machine. If it fails, use the **Open Zadig** button in the
app as a manual fallback.

This tool never flashes firmware — that stays in the Wingflight
Configurator. It only fixes the driver so the device can be seen at all.

## Download

Binaries are published on GitHub Releases:

```
https://github.com/WingFlight/wingflight-driver-fixer/releases
```

Asset name: `wingflight-driver-fixer-<version>-windows-<arch>.zip`

## Developer Notes

Run from source:

```
cd src
pip install -r requirements_driver_fixer.txt
python driver_fixer_gui.py
```

Installing a driver requires an elevated (Administrator) process — the app
shows a warning banner if it isn't running elevated.

Build a standalone EXE:

1. Windows build host (PyInstaller target).
2. Python 3.9+ on PATH.
3. PyInstaller installed: `pip install pyinstaller`
4. From `src`, run: `make.cmd`
5. Output EXE: `wingflight-driver-fixer.exe`
