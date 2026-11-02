# Esp32-Claude-HID

ESP32-C3 super mini acts as a Bluetooth LE keyboard + mouse for an offline Windows PC.
Claude drives it over USB serial from this PC and sees the offline PC's screen through a webcam.
Design: `docs/superpowers/specs/2026-11-02-esp32-ble-hid-agent-design.md`.

## Hardware: ONLY touch the project board

The project board is the ESP32-C3 super mini:

- Port: **COM31** (USB-Serial/JTAG, VID 303A / PID 1001)
- MAC: **a0:85:e3:4e:4f:bc**

Other ESP32 boards are plugged into this PC (e.g. COM19, VID 303A / PID 80C5). **Never connect to, open, reset,
flash, erase or monitor any port other than the project board.**

- Always pass the port explicitly (`--port COM31` / `-p COM31`). Never use port auto-detect
  (`arduino-cli board list` is fine for reading; `--port auto`, esptool without `--port`, or "first ESP found" logic are not).
- COM numbers can change after replugging. Before flashing, confirm the MAC is `a0:85:e3:4e:4f:bc`
  (`esptool --port COMxx read-mac`); if it doesn't match, stop and ask the user.
- Host code must take the port from config/argument, never scan for Espressif VIDs.

## Working on this repo

- BLE name: **ABKey HID**. The board pairs with one PC only; `python host/hidlink.py forget` releases it.
- Layout: `firmware/claude_hid/` (sketch), `host/hidlink.py` (serial + keymap), `host/camera.py`
  (capture/calibrate/record), `host/server.py` (MCP server + agent guide in `GUIDE`), `tests/test_host.py`.
- Tests (no hardware): `python -m pytest tests -q`. Build/flash steps: README section 3 (MAC check first).
- Local-only, never commit: `host/calibration.json`, `recordings/` (both gitignored).
- mcp is pinned to 2.x (`MCPServer`); tool errors must be raised as `ToolError` or the agent sees no message.
- Absolute mouse reaches only Windows' main display. Hardware tests must keep all input on the display the
  camera watches, inside a window you control.
