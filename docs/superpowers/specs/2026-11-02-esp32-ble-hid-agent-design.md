# ESP32 BLE HID agent bridge: design

Date: 2026-11-02 · Status: draft for approval

## Intent

The user works on an **offline Windows PC** (no network) on code, setting up new hardware and software
(drivers, installers, configuration), spreadsheets and PowerPoint. They want Claude to work alongside
them on that PC, without installing anything on it and without connecting it to a network.

Claude runs on a second PC ("this PC", the one with the repo). It gets:

- **Hands:** an ESP32-C3 super mini plugged into this PC over USB that pairs with the offline PC as an
  ordinary Bluetooth keyboard and mouse.
- **Eyes:** a webcam on this PC pointed at the offline PC's monitor, seeing the whole screen.

The offline PC sees only a standard Bluetooth keyboard and mouse, with no drivers and no software.

## Goals

1. Claude (Claude Code) can type text, press key combos, move, click, drag and scroll on the offline PC.
2. Claude can see the offline PC's screen as a flat, front-on image, and zoom into a region to read small text.
3. Clicks land where Claude aims: a point in the screenshot maps to the same point on the screen.
4. Every session is recorded to a video file on this PC so the user can review what the agent did.
5. The user can stop the agent instantly with a physical button (kill switch) and take over.
6. Setup is quick: flash once, pair once, calibrate the camera once, register the MCP server.

## Non-goals (for now)

- Agents other than Claude Code. MCP is a standard, so others may work, but they're untested and unsupported.
- Multiple monitors on the offline PC. Primary monitor only.
- Non-US keyboard layouts and non-ASCII typing.
- Clipboard or file transfer between the PCs.
- macOS/Linux as the offline PC (may work, not tested).
- Built-in OCR. Claude reads the images itself.

## Anti-goals: failure even if it "works"

- **Touching any other ESP32** on this PC (see `CLAUDE.md`: only COM31, MAC a0:85:e3:4e:4f:bc).
- Installing or requiring anything on the offline PC.
- The agent continuing to send input after the user hits the kill switch.
- **Stuck keys or buttons** (e.g. Ctrl held down on the user's PC after a crash or unplug).
- The agent acting on a stale screenshot (an old buffered camera frame).

Accepted by nature of the request: whatever is on the offline PC's screen is sent to Claude as camera images.

## Constraints and facts on the ground

- Board: ESP32-C3 (QFN32, rev v0.4), BLE 5 only (no Classic BT), 4 MB flash, native USB-Serial/JTAG on COM31.
- Toolchain already installed: Arduino CLI, `esp32:esp32` core 3.2.1, library NimBLE-Arduino 2.5.0.
- This PC: Windows 11, Python 3.12, ffmpeg, Node. Not yet installed: `pyserial`, `opencv-python`, `mcp`.
- Camera: the user will plug a USB webcam into this PC aimed at the offline monitor. The laptop's
  integrated camera is not the screen camera.
- Offline PC: Windows, US keyboard layout, single monitor, has Bluetooth LE.
- The user may use the offline PC's real keyboard and mouse at the same time as the agent.

## How it works

```
 Claude Code ──MCP (stdio)──> host server (Python, this PC) ──USB serial──> ESP32-C3 ──BLE HID──> offline PC
                                     ^                                                                │
                                     └──────────────── USB webcam (looks at the monitor) <────────────┘
```

### 1. Firmware (ESP32-C3, Arduino + NimBLE-Arduino)

**Decided:**

- **Dumb report forwarder.** The firmware knows nothing about text or layouts. The host sends raw HID
  reports over serial and the firmware sends them over BLE. All smarts (key maps, typing, scaling) live in
  Python, where they're easier to test and change.
- **BLE HID composite device** (HID over GATT):
  - Keyboard: standard 8-byte report (modifier byte, reserved, 6 key codes).
  - **Absolute mouse**: buttons (left/right/middle), X and Y as absolute 0–32767, and a relative wheel.
    Absolute because Windows pointer acceleration distorts relative mouse moves, and absolute moves
    land exactly with no "home to corner" tricks.
- Advertises with a fixed name (e.g. "Claude HID") and keyboard appearance. Bonding with "Just Works"
  pairing, bond stored in flash so it reconnects automatically.
- **One paired PC only** *(Claude's call)*. Once bonded, the board accepts only that PC. A serial
  command (`B`) clears the bond so you can re-pair. This stops a nearby device from pairing and
  receiving the agent's keystrokes.
- **Kill switch = BOOT button (GPIO9).** Pressing it toggles *paused*. While paused, the firmware
  releases all keys and buttons and rejects input commands with an error naming the kill switch. Pressing
  again resumes. The onboard LED shows the state (exact blink pattern: builder's call).
  **The paused state is saved to flash** *(Claude's call)*, so a reset doesn't silently resume the agent.
- **Stuck-input guard** *(Claude's call)*: if any key or button is held and no command arrives for
  ~200 ms, the firmware releases everything. That's below Windows' key-repeat delay, so a stuck key
  never auto-repeats. The host sends each release immediately after its press, so normal use never
  hits the guard. On BLE disconnect the firmware clears state.
- **Serial protocol:** line-based ASCII at 115200 (USB-CDC, so the baud is nominal). One command per line,
  one reply line per command (`OK ...` or `ERR <reason>`). The host waits for the reply before sending
  the next command, which gives natural flow control. Minimum set:
  - `K <mod> <k1> <k2> <k3> <k4> <k5> <k6>`: keyboard report (hex or decimal, builder's call)
  - `M <buttons> <x> <y> <wheel>`: absolute mouse report
  - `S`: status → `OK connected=<0|1> paused=<0|1>`
  - `R`: release everything
  - `B`: forget the bonded PC
- If not connected over BLE, `K`/`M` return `ERR not connected`.

### 2. Host library (Python, this PC)

**Decided:**

- **Serial link** to the ESP32 using `pyserial`. The port comes from config (default `COM31`) and is
  never auto-detected (see `CLAUDE.md`). The port is opened with DTR/RTS held inactive so that
  opening it doesn't reset the board.
- **Keyboard:** US-layout map from ASCII to (modifier, keycode). `type_text` sends press and release per
  character with a small configurable delay between reports (BLE can drop fast key-ups). `\n` → Enter,
  `\t` → Tab. Non-ASCII → error naming the character. Key combos are parsed from strings like
  `ctrl+shift+esc`, `win+r`, `alt+f4`, `f5`, `enter`.
- **Mouse:** screenshot coordinates (see camera) are scaled linearly to 0–32767. Supports move, click
  (left/right/middle, single/double), drag (press, move, release) and scroll.
- Always sends `R` (release all) on any error and on shutdown.

### 3. Camera (Python, OpenCV)

**Decided:**

- **One background capture thread** keeps reading frames so a screenshot is always the latest frame,
  never a stale buffered one.
- **Calibration, once per camera position:** a small command opens the live camera view in a window
  on this PC. The user clicks the four screen corners (TL, TR, BR, BL). That's saved to a JSON file (corner
  points plus the offline screen's aspect ratio). Manual corners are reliable regardless of wallpaper or
  lighting; auto-detection can be added later if this gets annoying.
- **Rectified screenshot:** a perspective warp of the latest frame using the calibration, giving a
  flat image with the screen's aspect ratio (e.g. 1280 px wide). Screenshot pixel (x, y) maps to the HID
  absolute position (x / W × 32767, y / H × 32767). So "click what you see at (x, y)" is exact by
  construction, independent of the offline PC's resolution.
- **Zoom:** a region of the screen (in screenshot coordinates) re-warped from the full-resolution
  camera frame at higher magnification, so Claude can read code and small text.
- **Recording:** while the server runs, the rectified view is written to an MP4 file (a few fps is
  enough) in a `recordings/` folder named by timestamp. If not calibrated, it records the raw camera frame.
- Camera index and resolution come from config. Request the highest resolution the webcam supports
  (1080p minimum recommended for legible text).

### 4. MCP server (what Claude uses)

**Decided:**

- A Python MCP server (official `mcp` SDK, FastMCP), stdio transport, registered in a project
  `.mcp.json` so Claude Code picks it up in this repo.
- Tools (names indicative):
  - `screenshot()` → image of the whole screen (rectified)
  - `zoom(x, y, width, height)` → magnified image of a region
  - `click(x, y, button="left", double=false)`, `move(x, y)`, `drag(x1, y1, x2, y2)`, `scroll(x, y, amount)`
  - `type_text(text)`, `key(combo)` (e.g. `"ctrl+s"`)
  - `status()` → connected/paused/calibrated/recording
- Tool errors are plain messages Claude can act on, e.g. "paused: the user pressed the kill switch.
  Wait for them to resume" or "not connected: pair 'Claude HID' on the offline PC".
- **The usage guide lives in the server's `instructions` field**, so any client connecting gets it.
  It covers: take a screenshot after every action and verify; use `zoom` before reading small text;
  prefer keyboard shortcuts over hunting with the mouse; the user may be using the PC too, so look
  before acting; stop when paused; how typing interacts with editors (below).
- A `README.md` for the human: flash, pair, calibrate, register, troubleshoot.

### Known issue to document: typing code into editors

Editors like VS Code auto-indent and auto-close brackets, so typed code comes out mangled. The guide tells
Claude to either type into a plain editor (Notepad), or (with the user's OK) turn off
`editor.autoIndent`, `editor.autoClosingBrackets`, `editor.autoClosingQuotes` and `editor.formatOnType`
on the offline PC.

## Risks: verify first

1. **Absolute mouse over BLE on Windows.** Windows is expected to accept an absolute-pointer mouse over
   HID-over-GATT. **Spike this first** (pair, move to a few absolute positions, eyeball them). Fallback if
   it fails: a relative mouse, with the guide telling the user to turn off "Enhance pointer precision",
   and the host homing to the top-left corner before each move.
2. **Typing reliability over BLE** (dropped or repeated keys at speed). Tune the per-report delay; verify by
   typing a known 500-character string into Notepad and comparing it via a zoomed screenshot.
3. **Camera legibility** depends on the webcam. 1080p+ and a fixed mount are recommended.

## Left to the builder

- File layout, module names, exact config format (a small JSON or TOML next to the server is fine).
- Exact report descriptor bytes, LED pattern, reply formats beyond those above, timing constants.
- Whether `.mcp.json` launches the server via `python <path>` or `python -m`.
- Test approach: at least one runnable check for the key map and the coordinate scaling (pure Python,
  no hardware). Hardware steps are verified manually per the risks above.
- Build/flash: `arduino-cli` with FQBN `esp32:esp32:esp32c3` and USB-CDC-on-boot enabled, **port COM31
  only**, MAC checked first (see `CLAUDE.md`).
