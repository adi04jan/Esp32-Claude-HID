# Esp32-Claude-HID

Let Claude work on an **offline Windows PC** with you. An ESP32-C3 super mini pairs with that PC as an
ordinary Bluetooth keyboard and mouse, and a webcam watches its screen. Claude drives both from *this* PC
through an MCP server. Nothing is installed on the offline PC, and it never touches a network.

```
Claude Code ─MCP─> host/server.py ─USB serial─> ESP32-C3 ─Bluetooth HID─> offline PC
                        ^                                                     │
                        └──────────── USB webcam looking at its monitor <──────┘
```

## 1. Hardware

- ESP32-C3 super mini on **COM31** (MAC `a0:85:e3:4e:4f:bc`). Other ESP32s on this PC are off-limits; see `CLAUDE.md`.
- A **1080p or better USB webcam** on a fixed mount (tripod or clamp), seeing the whole offline monitor.
  If the camera moves, recalibrate. The closer it fills the frame, the more legible small text is.
- **Several monitors?** The Bluetooth mouse reaches only Windows' **main display**, so aim the camera at that one.

## 2. Install

```
python -m pip install -r host/requirements.txt
```

## 3. Flash the ESP32 (once)

Arduino CLI with the `esp32:esp32` core and the NimBLE-Arduino library. **Check the MAC first.** COM numbers
can change, and flashing the wrong board overwrites it.

```
esptool --port COM31 read-mac          # must print a0:85:e3:4e:4f:bc
arduino-cli compile --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc firmware/claude_hid
arduino-cli upload  -p COM31 --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc firmware/claude_hid
```

## 4. Pair with the offline PC

On the offline PC: **Settings → Bluetooth & devices → Add device → Bluetooth → "ABKey HID"**.
Check with `python host/hidlink.py status`, which should show `connected=True bonded=True`.

The board accepts **one PC only**. If it was ever paired with another PC (e.g. during testing), run
`forget` first, or the new PC will pair and immediately disconnect. To move it to another PC:

```
python host/hidlink.py forget        # board forgets the old PC
```

Then remove "ABKey HID" from the old PC's Bluetooth devices, and pair the new one. If you change the
firmware's report descriptor, remove and re-pair too, because Windows caches it.

## 5. Calibrate the camera (once per camera position)

```
python host/camera.py list                                   # find the webcam's index
python host/camera.py calibrate --camera 1 --screen 1920x1080  # offline PC's resolution
```

Click the screen's four corners in the order **top-left, top-right, bottom-right, bottom-left**. Press
`r` to restart and **Enter** to save. A flattened preview appears; check it shows the whole screen.
Close Claude Code first, because only one program can use the webcam at a time.

Until you calibrate, the server opens **no** camera at all. It won't guess an index, because index 0 is
usually the laptop's own face camera.

## 6. Use it from Claude Code

Start Claude Code in this folder. `.mcp.json` registers the `desktop-hid` server automatically (approve it
once). Then ask, e.g. *"Take a screenshot of the offline PC and open Notepad."* The server's built-in guide
tells Claude how to work: verify after every action, zoom to read text, prefer shortcuts.

To use it from another folder:

```
claude mcp add desktop-hid -- python D:\Personal\Project\Esp32-Claude-HID\host\server.py
```

**Other MCP clients** (Claude Desktop, other agents): it's a standard stdio MCP server. Add:

```json
{ "mcpServers": { "desktop-hid": { "command": "python",
    "args": ["D:\\Personal\\Project\\Esp32-Claude-HID\\host\\server.py"] } } }
```

The agent guide (verify after every action, zoom before reading, shortcuts first, stop when paused, typing
rules for code editors) is sent as the server's MCP `instructions`, so every client gets it. There's no
separate prompt to copy.

| Tool | What it does |
|------|--------------|
| `screenshot` | flattened photo of the whole screen; its pixels are the click coordinates |
| `zoom(x, y, width, height)` | magnified region for reading small text |
| `click`, `move`, `drag`, `scroll` | mouse, in screenshot pixels |
| `type_text(text)` | US-layout ASCII, at most 2000 characters per call |
| `key(combo, repeat)` | e.g. `ctrl+s`, `win+r`, `alt+tab`, `f5` |
| `status` | link, kill switch, camera, recording |

Pressing Esc in Claude Code interrupts a running `type_text` or click within one keystroke, and any held
key is released.

## 7. Kill switch and LED

Press **BOOT** on the super mini to pause all agent input instantly. Held keys are released and Claude is
told how much it had typed, then to wait. Press it again to resume. The pause survives a reset or replug, and
pressing BOOT never moves your mouse pointer.

| LED | Meaning |
|-----|---------|
| fast blink (5 Hz) | paused (kill switch) |
| solid | connected, agent can type and click |
| slow blink (1 Hz) | not connected; advertising for pairing |

Safety nets: any key or button held for over 200 ms without a new command is released automatically.
Opening the serial port doesn't reset the board.

## 8. Recordings

Every session is recorded to `recordings/YYYYmmdd-HHMMSS.mkv`: the flattened screen at 5 fps, in
10-minute files. Matroska stays playable even if the server is killed mid-session; at most the last
second or two is lost. Play them in VLC. Settings are in `host/config.json`.

## 9. Troubleshooting

- **"not connected"**: the offline PC is off or asleep, out of range, or not paired. Check the LED.
- **Pairs, then disconnects at once**: the board is still bonded to a different PC. Run
  `python host/hidlink.py forget`, remove "ABKey HID" on both PCs, then pair again.
- **`hidlink.py` says "Access is denied"**: a Claude session holds COM31. Close it, or use the `status` tool.
- **"send failed"**: the Bluetooth link is congested or weak. The board keeps retrying; move it closer.
- **"camera not calibrated"**: run step 5. This also happens if the webcam's resolution changed.
- **Webcam plugged in after Claude started**: run `/mcp` in Claude Code and reconnect `desktop-hid`.
- **"can't open camera N"**: the webcam is unplugged or another app is using it. After replugging, run
  `python host/camera.py list`. If its index changed, recalibrate.
- **Missing or doubled characters**: raise `key_delay_ms` in `host/config.json` (default 12; keep it under 100).
- **Typed code is mangled in VS Code**: auto-indent and auto-close brackets. Type into Notepad, or turn off
  `editor.autoIndent`, `editor.autoClosingBrackets`, `editor.autoClosingQuotes` and `editor.formatOnType`.
- **Clicks land off target**: the camera moved, so recalibrate. With several monitors, the mouse covers the primary one only.
- Run the self-checks (no hardware needed): `python -m pytest tests -q`.

## 10. What's been verified on real hardware

Windows 11 PC, ESP32-C3 super mini, Logitech 1080p webcam viewing a 1920×1080 main display at an angle:

| Check | Result |
|-------|--------|
| Typing 500 characters, every printable symbol plus Tab | 500/500 exact |
| Click lands where aimed (Bluetooth absolute mouse) | within ±2 px, including all 4 corners |
| End-to-end click accuracy (depends on corner-click calibration) | 3–20 px |
| Kill switch pressed mid-typing | stopped after 110 chars, no repeats, agent told "typed 110 of 1760" |
| Pause survives a board reset; opening the port doesn't reset it | yes; yes |
| BOOT doesn't move the owner's pointer | yes |
| Webcam unplugged | clicks refused ("can't open camera 1"), nothing sent |
| Recording survives the server being killed | yes (`.mkv`) |
