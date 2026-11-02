# ESP32 BLE HID Agent Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Claude Code drive an offline Windows PC through an ESP32-C3 BLE keyboard + absolute mouse, and see it through a webcam, via an MCP server.

**Architecture:** The firmware is a dumb BLE HID report forwarder with a line-based serial protocol, a kill switch and a stuck-key guard. Python on this PC does everything else: key maps, coordinate scaling, camera rectification, recording. The MCP server exposes it to Claude, and its `instructions` field carries the agent guide.

**Tech Stack:** Arduino CLI + esp32 core 3.2.1 + NimBLE-Arduino 2.5.0 (already installed); Python 3.12 with pyserial, opencv-python, numpy, mcp (FastMCP) and pytest.

**Spec:** `docs/superpowers/specs/2026-11-02-esp32-ble-hid-agent-design.md`

## Global Constraints

- **Only the board on COM31 with MAC `a0:85:e3:4e:4f:bc` may be touched.** Never auto-detect ports. Check the MAC before every flash. See `CLAUDE.md`.
- FQBN: `esp32:esp32:esp32c3:CDCOnBoot=cdc`. BLE name: `Claude HID`.
- Serial: 115200, one command per line, one reply per command (`OK ...` / `ERR <reason>`). The host ignores reply lines that start with neither.
- HID absolute range: 0–32767 on both axes.
- Stuck-input guard: 200 ms. Kill switch: BOOT = GPIO9 (active low). LED: GPIO8 (active low).
- US layout, ASCII only. Single monitor on the offline PC.
- Screenshot long edge must stay ≤ 1280 px, so the client never resizes images and Claude's pixel coordinates stay exact.
- Nothing is installed on the offline PC.
- Work on branch `feat/ble-hid-agent`.

## Review Focus

1. **Kill switch pressed mid-`type_text`:** typing stops at the next character, the tool returns the "paused" message, `R` is sent, and no key is left held. (Task 2 test)
2. **Text with CRLF line endings or non-ASCII** (smart quotes or em-dashes pasted by the model): CRLF types one Enter. Any non-ASCII character is rejected *before anything is typed*, and the error names the character. (Task 2 test)
3. **Coordinates outside the screenshot** (e.g. zoom-image coordinates used by mistake): a clear error states the valid range, and nothing is sent. (Task 4 test)
4. **Click before calibration, or calibration saved at a different camera resolution:** a clear "run calibrate" error, and nothing is sent. (Task 3 test)
5. **ESP32 unplugged and replugged mid-session:** the next command reopens the port once and retries. Opening the port doesn't reset the board, and pause survives a reset. (Task 2 test + Task 1 manual check)

## File map

```
firmware/claude_hid/claude_hid.ino   BLE HID + serial protocol + kill switch + guard
host/hidlink.py                      serial link, US key map, combos, mouse scaling, CLI
host/camera.py                       capture thread, calibration, rectify/zoom, recording, CLI
host/server.py                       FastMCP server, agent guide, tools
host/config.json                     defaults (committed)
host/requirements.txt
tests/test_host.py                   pure-Python checks (no hardware)
.mcp.json  README.md  .gitignore     (.gitignore: host/calibration.json, recordings/, __pycache__/)
```

`host/config.json`:
```json
{ "port": "COM31", "key_delay_ms": 12, "camera_width": 1920, "camera_height": 1080,
  "shot_width": 1280, "record_fps": 5, "record_segment_min": 10 }
```

---

### Task 1: Firmware + absolute-mouse spike

**Files:**
- Create: `firmware/claude_hid/claude_hid.ino`, `host/requirements.txt`, `.gitignore`

**Interfaces:**
- Produces the serial protocol that every later task uses:
  - `K <mod> <k1> <k2> <k3> <k4> <k5> <k6>` (decimal) → `OK`
  - `M <buttons> <x> <y> <wheel>` (decimal; buttons bit0=L bit1=R bit2=M; x,y 0–32767; wheel −127..127) → `OK`
  - `S` → `OK connected=<0|1> paused=<0|1> bonded=<0|1>` (`connected` = encrypted and allowed to receive input)
  - `R` → `OK` (release all; always allowed, even when paused)
  - `B` → `OK` (delete all bonds, disconnect)
  - Errors: `ERR paused`, `ERR not connected`, `ERR bad command`

- [ ] **Step 1: Branch and deps**

Run: `git checkout -b feat/ble-hid-agent`. Then write `host/requirements.txt` (`pyserial`, `opencv-python`, `numpy`, `mcp`, `pytest`) and run `python -m pip install -r host/requirements.txt`. Write `.gitignore` per the file map.
Expected: `python -c "import serial, cv2, mcp, pytest"` exits 0.

- [ ] **Step 2: Write the sketch**

Check the NimBLE API against the installed header `<arduino-cli config get directories.user>\libraries\NimBLE-Arduino\src\NimBLEHIDDevice.h` (2.x uses `setManufacturer`, `setPnp`, `setHidInfo`, `setReportMap`, `getInputReport(id)`, `startServices`, `setBatteryLevel`, `getHidService`). Required behaviour:

- Report map, used verbatim. Report 1 is the keyboard (8 bytes). Report 2 is the absolute mouse (6 bytes: buttons, xLo, xHi, yLo, yHi, wheel):
```c
0x05,0x01,0x09,0x06,0xA1,0x01,0x85,0x01, 0x05,0x07,0x19,0xE0,0x29,0xE7,0x15,0x00,0x25,0x01,0x75,0x01,0x95,0x08,0x81,0x02,
0x95,0x01,0x75,0x08,0x81,0x01, 0x95,0x06,0x75,0x08,0x15,0x00,0x25,0x73,0x05,0x07,0x19,0x00,0x29,0x73,0x81,0x00, 0xC0,
0x05,0x01,0x09,0x02,0xA1,0x01,0x85,0x02, 0x09,0x01,0xA1,0x00,
  0x05,0x09,0x19,0x01,0x29,0x03,0x15,0x00,0x25,0x01,0x95,0x03,0x75,0x01,0x81,0x02, 0x95,0x01,0x75,0x05,0x81,0x01,
  0x05,0x01,0x09,0x30,0x09,0x31,0x16,0x00,0x00,0x26,0xFF,0x7F,0x75,0x10,0x95,0x02,0x81,0x02,
  0x09,0x38,0x15,0x81,0x25,0x7F,0x75,0x08,0x95,0x01,0x81,0x06,
0xC0,0xC0
```
- Security: `setSecurityAuth(true, false, true)` and IO cap `NO_INPUT_OUTPUT`. Advertise the HID service UUID with keyboard appearance (0x03C1). Re-advertise on disconnect.
- **Bond policy:** set `ready = true` in `onAuthenticationComplete` only if encryption succeeded and `NimBLEDevice::getNumBonds() <= 1`. Otherwise delete that peer's bond and disconnect. Clear `ready` and held state on disconnect.
- `paused` is stored in `Preferences` (namespace `hid`, key `paused`) and loaded in `setup()`. BOOT press (50 ms debounce) toggles it, saves it and releases all.
- Guard: if any key, modifier or button is held and no serial command has arrived for 200 ms, release all.
- LED: paused → 5 Hz blink; ready → solid on; otherwise 1 Hz blink.
- `K`/`M` → `ERR paused` if paused, else `ERR not connected` if not ready, else notify the report and reply `OK`. The firmware prints nothing except replies.

- [ ] **Step 3: Compile**

Run: `arduino-cli compile --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc firmware/claude_hid`
Expected: success, sketch size printed.

- [ ] **Step 4: Verify the MAC, then flash COM31 only**

Run the Arduino-bundled esptool: `esptool --port COM31 read-mac`. Expected: `a0:85:e3:4e:4f:bc`. **If it differs, stop and ask the user.**
Then run `arduino-cli upload -p COM31 --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc firmware/claude_hid`.

- [ ] **Step 5: Protocol check (no BLE yet)**

Open COM31 with pyserial with `dtr=False, rts=False` set *before* `open()`. Send `S`, `K 0 4 0 0 0 0 0`, `X`.
Expected: `OK connected=0 paused=0 bonded=0`, then `ERR not connected`, then `ERR bad command`.
Ask the user to press BOOT, then send `S`. Expected: `paused=1`. Ask them to press RST, reopen the port and send `S`. Expected: still `paused=1`. Ask them to press BOOT again to resume.

- [ ] **Step 6: Absolute-mouse spike on this PC** (the spec's first risk)

Ask the user to pair "Claude HID" in this PC's Bluetooth settings. Then send `S` and expect `connected=1 bonded=1`.
Send `M 0 0 0 0`, `M 0 32767 32767 0` and `M 0 16384 16384 0`. After each, read the pointer with PowerShell: `Add-Type -A System.Windows.Forms; [Windows.Forms.Cursor]::Position; [Windows.Forms.Screen]::PrimaryScreen.Bounds`.
Expected: top-left, bottom-right and centre of the primary screen, within ±2 px.
**If the pointer doesn't move or lands elsewhere, stop and report.** The fallback (relative mouse) is a design change for the user to approve.

- [ ] **Step 7: Unpair from this PC**

Send `B`, and ask the user to remove "Claude HID" from this PC's Bluetooth devices. Windows caches the report map, so a stale pairing must never be reused.

- [ ] **Step 8: Commit**

```bash
git add firmware host/requirements.txt .gitignore CLAUDE.md docs
git commit -m "feat(firmware): BLE HID keyboard + absolute mouse bridge with kill switch"
```

---

### Task 2: Host HID link

**Files:**
- Create: `host/hidlink.py`, `host/config.json`, `tests/test_host.py`

**Interfaces:**
- Consumes: the Task 1 protocol.
- Produces (in `host/hidlink.py`):
  - `char_to_key(ch: str) -> tuple[int, int]`: (modifier, keycode), US layout. `\n`→Enter (0x28), `\t`→Tab (0x2B). Raises `ValueError` naming the char.
  - `parse_combo(combo: str) -> tuple[int, int]`: case-insensitive, `+`-separated. Modifiers `ctrl|control`=0x01, `shift`=0x02, `alt`=0x04, `win|gui|meta`=0x08. Named keys: enter/return 0x28, esc/escape 0x29, backspace 0x2A, tab 0x2B, space 0x2C, capslock 0x39, f1–f12 0x3A–0x45, printscreen 0x46, insert 0x49, home 0x4A, pageup 0x4B, delete/del 0x4C, end 0x4D, pagedown 0x4E, right 0x4F, left 0x50, down 0x51, up 0x52, menu 0x65. A single char goes through `char_to_key`, and its shift is ORed in. A modifier-only combo gives keycode 0. An unknown name raises `ValueError` naming it.
  - `to_abs(frac: float) -> int`: `round(frac * 32767)`. Raises `ValueError` outside [0, 1].
  - `class HidError(RuntimeError)`: message is agent-readable. `ERR paused` → "paused: the user pressed the kill switch on the ESP32. Stop and wait for them to resume; don't retry." `ERR not connected` → "not connected: pair 'Claude HID' on the offline PC (or it's out of range)."
  - `class HidLink(port: str, key_delay: float = 0.012, ser=None)` with methods: `cmd(line) -> str`, `status() -> dict[str, bool]`, `release()`, `forget()`, `type_text(text)`, `key(combo, repeat=1)`, `move(fx, fy)`, `click(fx, fy, button="left", double=False)`, `drag(fx1, fy1, fx2, fy2)`, `scroll(fx, fy, clicks)` (positive = up), `close()`. Mouse methods take fractions 0–1 of the screen and track the last position, so every `M` report carries the absolute x, y. `ser` injects an open serial-like object (tests). `_open()` creates the real port.
  - CLI: `python host/hidlink.py [--port COM31] status|forget|release|raw "<line>"`

- [ ] **Step 1: Write the failing tests** in `tests/test_host.py` (add `host/` to `sys.path` at the top). `FakeSerial(replies=None, fail_first_write=False)` has `write(b)`, which records decoded lines in `.lines` and raises `serial.SerialException` once if `fail_first_write`; `readline()`, which pops scripted replies (default `b"OK\n"`); and `close()`.

```python
def test_char_to_key():
    assert char_to_key("a") == (0, 0x04) and char_to_key("A") == (0x02, 0x04)
    assert char_to_key("!") == (0x02, 0x1E) and char_to_key("0") == (0, 0x27)
    assert char_to_key("\n") == (0, 0x28) and char_to_key("~") == (0x02, 0x35)
    with pytest.raises(ValueError, match="é"): char_to_key("é")

def test_parse_combo():
    assert parse_combo("ctrl+shift+esc") == (0x03, 0x29)
    assert parse_combo("Win+R") == (0x08, 0x15)
    assert parse_combo("f5") == (0, 0x3E)
    assert parse_combo("ctrl+A") == (0x03, 0x04)
    assert parse_combo("win") == (0x08, 0)
    with pytest.raises(ValueError, match="foo"): parse_combo("ctrl+foo")

def test_to_abs():
    assert (to_abs(0), to_abs(1), to_abs(0.5)) == (0, 32767, 16384)
    with pytest.raises(ValueError): to_abs(1.01)

def test_type_text_crlf_is_one_enter():
    ser = FakeSerial(); HidLink("X", 0, ser).type_text("a\r\nb")
    assert ser.lines == ["K 0 4 0 0 0 0 0", "K 0 0 0 0 0 0 0", "K 0 40 0 0 0 0 0",
                         "K 0 0 0 0 0 0 0", "K 0 5 0 0 0 0 0", "K 0 0 0 0 0 0 0"]

def test_non_ascii_rejected_before_typing():
    ser = FakeSerial()
    with pytest.raises(ValueError, match="“"): HidLink("X", 0, ser).type_text("ok “quote”")
    assert ser.lines == []

def test_kill_switch_mid_typing_stops_and_releases():
    ser = FakeSerial(replies=[b"OK\n", b"OK\n", b"ERR paused\n"])
    with pytest.raises(HidError, match="kill switch"): HidLink("X", 0, ser).type_text("abc")
    assert ser.lines == ["K 0 4 0 0 0 0 0", "K 0 0 0 0 0 0 0", "K 0 5 0 0 0 0 0", "R"]

def test_reconnect_once_on_serial_error():
    ser = FakeSerial(replies=[b"OK connected=1 paused=0 bonded=1\n"], fail_first_write=True)
    link = HidLink("X", 0, ser); link._open = lambda: ser
    assert link.status() == {"connected": True, "paused": False, "bonded": True}

def test_click_sends_absolute_position():
    ser = FakeSerial(); HidLink("X", 0, ser).click(0.5, 1.0)
    assert ser.lines == ["M 0 16384 32767 0", "M 1 16384 32767 0", "M 0 16384 32767 0"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_host.py -v`
Expected: FAIL (ImportError: `hidlink`).

- [ ] **Step 3: Implement `host/hidlink.py` and `host/config.json`**

Rules:
- `_open()` creates `serial.Serial()`, sets port, baud, `timeout=2`, `dtr=False`, `rts=False`, then `open()`.
- `cmd()` holds a `threading.Lock`. On `SerialException`/`OSError` it closes, calls `_open()` once and retries.
- `type_text` validates every char first (dropping `\r` when it's followed by `\n`), then sends press, waits `key_delay`, sends release and waits.
- Any `HidError` raised inside a multi-report action sends `R` (best effort) and re-raises.
- `close()` sends `R` and then closes.

- [ ] **Step 4: Run the tests to see them pass**

Run: `python -m pytest tests/test_host.py -v`
Expected: all PASS.

- [ ] **Step 5: Hardware smoke test**

Run: `python host/hidlink.py status`
Expected: `connected=False paused=False bonded=False` (unpaired after Task 1). The board must not reset when the port opens, so the LED keeps its 1 Hz blink with no reboot gap.

- [ ] **Step 6: Commit**

`git add host/hidlink.py host/config.json tests/test_host.py && git commit -m "feat(host): serial HID link with US keymap and combos"`

---

### Task 3: Camera: capture, calibration, rectify, zoom, recording

**Files:**
- Create: `host/camera.py`
- Modify: `tests/test_host.py` (append)

**Interfaces:**
- Consumes: `host/config.json` keys `camera_width`, `camera_height`, `shot_width`, `record_fps` and `record_segment_min`.
- Produces (in `host/camera.py`):
  - `class CameraError(RuntimeError)`
  - `shot_size(shot_width: int, screen: tuple[int, int]) -> tuple[int, int]`: height = `round(shot_width * sh / sw)`.
  - `screen_matrix(corners: list[list[float]], size: tuple[int, int]) -> np.ndarray`: corners in TL, TR, BR, BL order, mapped to `(0,0), (W-1,0), (W-1,H-1), (0,H-1)` via `cv2.getPerspectiveTransform`.
  - `zoom_matrix(M, x, y, w, h, scale) -> np.ndarray`: `S(scale) @ T(-x, -y) @ M`. Re-warping the full-resolution frame with it gives the zoomed region.
  - `load_calibration(path: Path) -> dict | None`: JSON `{"camera": int, "camera_size": [w, h], "screen": [w, h], "corners": [[x, y] ×4]}`.
  - `class Camera(cfg: dict, calib_path: Path, rec_dir: Path)` with methods and properties: `start()`, `stop()`, `calibrated: bool`, `size: tuple[int, int]` (screenshot size), `screenshot() -> np.ndarray`, `zoom(x, y, w, h) -> tuple[np.ndarray, float]` (image, scale), `recording: Path | None`, `require_calibrated()` (raises `CameraError`). It must be constructible without `start()`, and tests may assign a frame to `_frame`.
  - CLI: `python host/camera.py list` (probes indexes 0–4 and prints which open and at what size) and `python host/camera.py calibrate --camera N --screen 1920x1080`.

- [ ] **Step 1: Append the failing tests** (`CFG = json.load(open("host/config.json"))`)

```python
def test_shot_size():
    assert shot_size(1280, (1920, 1080)) == (1280, 720)
    assert shot_size(1280, (1280, 1024)) == (1280, 1024)

def test_screen_matrix_maps_corners():
    c = [[100, 50], [900, 80], [880, 600], [120, 560]]
    out = cv2.perspectiveTransform(np.float32([c]), screen_matrix(c, (1280, 720)))[0]
    assert np.allclose(out, [[0, 0], [1279, 0], [1279, 719], [0, 719]], atol=1e-3)

def test_zoom_matrix_maps_region_to_output():
    c = [[0, 0], [1279, 0], [1279, 719], [0, 719]]; M = screen_matrix(c, (1280, 720))
    Z = zoom_matrix(M, 100, 200, 320, 180, 4.0)
    out = cv2.perspectiveTransform(np.float32([[[100, 200], [420, 380]]]), Z)[0]
    assert np.allclose(out, [[0, 0], [1280, 720]], atol=1e-3)

def test_calibration_from_other_camera_size_is_rejected(tmp_path):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"camera": 1, "camera_size": [1280, 720], "screen": [1920, 1080],
                             "corners": [[0, 0], [1279, 0], [1279, 719], [0, 719]]}))
    cam = Camera(CFG, p, tmp_path); cam._frame = np.zeros((1080, 1920, 3), np.uint8)
    assert not cam.calibrated
    with pytest.raises(CameraError, match="calibrate"): cam.require_calibrated()
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_host.py -v -k "shot or matrix or calibration"`
Expected: FAIL (ImportError: `camera`).

- [ ] **Step 3: Implement `host/camera.py`**

Rules:
- **Open:** `cv2.VideoCapture(index, cv2.CAP_DSHOW)`. Set FOURCC `MJPG` *before* width and height, otherwise USB2 webcams fall back to 5 fps at 1080p. The camera index comes from the calibration, or 0 if there is none.
- **Capture thread:** a daemon thread reads continuously and stores the latest frame under a lock. `screenshot()` raises `CameraError` if no frame arrives within 3 s.
- **Calibration validity:** calibrated means the calibration exists *and* `camera_size` equals the actual frame size. When uncalibrated, `screenshot()` returns the raw frame resized to `shot_width`, and `require_calibrated()` raises: "camera not calibrated for this setup — run: python host/camera.py calibrate --camera N --screen WxH (stop the Claude session first; the camera can only be opened by one program)".
- **Zoom:** scale = `1280 / max(w, h)`, so the output's long edge is 1280. The region must lie inside the screenshot with `w, h >= 8`, otherwise `ValueError`.
- **Recording:** the capture thread writes `screenshot()`-equivalent frames at `record_fps` with `cv2.VideoWriter` (`mp4v`) to `recordings/YYYYmmdd-HHMMSS.mp4`. It starts a new file every `record_segment_min` minutes, so a killed process loses at most one segment. `stop()` releases the writer.
- **Calibrate CLI:** show the live view scaled to fit 1280 wide. Left-click the corners in TL, TR, BR, BL order (numbered dots), `r` resets, Enter saves (corners scaled back to full-resolution coordinates) and Esc cancels. After saving, show the rectified preview until a key is pressed.

- [ ] **Step 4: Run the tests to see them pass**

Run: `python -m pytest tests/test_host.py -v`
Expected: all PASS.

- [ ] **Step 5: Hardware smoke test**

Run: `python host/camera.py list`
Expected: at least index 0 (integrated camera) listed with its size. Tell the user which index is new once the webcam is plugged in. Calibration itself happens in Task 5 with the offline PC.

- [ ] **Step 6: Commit**

`git add host/camera.py tests/test_host.py && git commit -m "feat(host): camera capture, calibration, rectified screenshots, recording"`

---

### Task 4: MCP server, agent guide, registration, README

**Files:**
- Create: `host/server.py`, `.mcp.json`, `README.md`
- Modify: `tests/test_host.py` (append)

**Interfaces:**
- Consumes: `HidLink`, `HidError` (Task 2); `Camera`, `CameraError` (Task 3); `host/config.json`.
- Produces:
  - `shot_to_frac(x: int, y: int, size: tuple[int, int]) -> tuple[float, float]`: `(x/(W-1), y/(H-1))`. Raises `ValueError("x,y must be inside the screenshot: 0..W-1, 0..H-1 (zoom coordinates are not click coordinates)")` with the real numbers filled in.
  - `mcp = FastMCP("desktop-hid", instructions=GUIDE)`. Tools:
    - `screenshot()` → `[text "screen WxH; click coordinates are pixels in this image", Image]`
    - `zoom(x, y, width, height)` → `[text "zoom ×s of (x,y,w,h); a point (zx,zy) here is (x+zx/s, y+zy/s) on the screenshot", Image]`
    - `click(x, y, button="left", double=False)`, `move(x, y)`, `drag(x1, y1, x2, y2)`, `scroll(x, y, direction: "up"|"down", clicks=3)`
    - `type_text(text)`, `key(combo, repeat=1)`
    - `status()` → text listing hid connected/paused/bonded, camera index/calibrated/size and the recording path
  - Images are JPEG quality 80 via `Image(data=..., format="jpeg")`.
  - `main()` loads config, builds `HidLink` (lazy port open) and `Camera`, starts the camera (a failure is reported by tools, not fatal), runs `mcp.run()` in `try/finally`, and stops the camera and closes the link.
  - Tool errors: `HidError`, `CameraError` and `ValueError` are returned as plain messages.

- [ ] **Step 1: Append the failing tests**

```python
def test_shot_to_frac():
    assert shot_to_frac(0, 0, (1280, 720)) == (0.0, 0.0)
    assert shot_to_frac(1279, 719, (1280, 720)) == (1.0, 1.0)
    with pytest.raises(ValueError, match="inside the screenshot"): shot_to_frac(1280, 10, (1280, 720))

def test_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"screenshot", "zoom", "click", "move", "drag", "scroll", "type_text", "key", "status"}
```
Importing `server` must not open the serial port or the camera; only `main()` does.

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_host.py -v -k "frac or tools"`
Expected: FAIL (ImportError: `server`).

- [ ] **Step 3: Implement `host/server.py`**

Every mouse tool calls `cam.require_calibrated()` and `shot_to_frac` before sending anything (Review Focus 3, 4). `GUIDE` must cover:
- what the setup is: a camera view of a real offline PC, plus a Bluetooth keyboard and mouse;
- take a screenshot after every action and verify before the next;
- use `zoom` before reading code or small text;
- prefer keyboard shortcuts (Win+R, Ctrl+L, Alt+Tab, Ctrl+S) over hunting with the mouse;
- the user may be using the PC too, so look before acting and never fight them for focus;
- on "paused", stop and wait for the user;
- in code editors, auto-indent and auto-closing brackets mangle typed code: type into Notepad, or ask the user before turning off VS Code's `editor.autoIndent`, `editor.autoClosingBrackets`, `editor.autoClosingQuotes` and `editor.formatOnType`;
- US layout, ASCII only;
- passwords and secrets are typed only when the user asks.

- [ ] **Step 4: Write `.mcp.json` and `README.md`**

`.mcp.json`:
```json
{ "mcpServers": { "desktop-hid": { "command": "python", "args": ["host/server.py"] } } }
```
README sections:
1. Hardware: ESP32-C3 super mini on COM31; a 1080p+ webcam on a fixed mount aimed at the offline monitor.
2. Install: `pip install -r host/requirements.txt`.
3. Flash, with the MAC check from `CLAUDE.md`.
4. Pair "Claude HID" on the offline PC. Re-pair means `python host/hidlink.py forget` plus removing the device in Windows. Changing the report map requires a re-pair.
5. `python host/camera.py list`, then `calibrate`.
6. Use it from Claude Code in this folder. Elsewhere: `claude mcp add desktop-hid -- python <abs path>\host\server.py`.
7. Kill switch and LED meanings.
8. Recordings in `recordings/`, playable in VLC.
9. Troubleshooting.

- [ ] **Step 5: Run the tests to see them pass**

Run: `python -m pytest tests/test_host.py -v`
Expected: all PASS.

- [ ] **Step 6: Check that Claude Code sees the server**

Run: `claude mcp list` from the repo root.
Expected: `desktop-hid` listed as connected. If it fails because of the working directory, switch `args` to the absolute path.

- [ ] **Step 7: Commit**

`git add host/server.py .mcp.json README.md tests/test_host.py && git commit -m "feat: MCP server with agent guide, project registration and README"`

---

### Task 5: End-to-end on the offline PC (with the user)

**Files:** none new. Fix-ups go in the files they touch.

- [ ] **Step 1: Pair.** The user pairs "Claude HID" on the offline PC. `python host/hidlink.py status` → `connected=True bonded=True`.
- [ ] **Step 2: Calibrate.** The user mounts the webcam. Run `python host/camera.py calibrate --camera <N> --screen <offline resolution>`. The rectified preview shows the whole screen, flat.
- [ ] **Step 3: Click accuracy.** In a Claude session, take a `screenshot` and click the Start button, then a desktop icon, then hover over the close button of a maximized window (don't click). Expected: each lands on target, confirmed by the next screenshot.
- [ ] **Step 4: Typing reliability** (spec risk 2). Open Notepad via `key("win+r")` and `type_text("notepad\n")`. Type a fixed 500-character ASCII string with all symbol keys, `zoom` in and compare. Expected: exact match. If not, raise `key_delay_ms` in `host/config.json` and repeat.
- [ ] **Step 5: Kill switch.** Start typing a long string and have the user press BOOT. Expected: typing stops, the tool returns the paused message and no key repeats. The user presses BOOT again and `status` shows `paused=False`.
- [ ] **Step 6: Recording.** Stop the session. Expected: the MP4 in `recordings/` plays in VLC.
- [ ] **Step 7: Commit any tuning** (`key_delay_ms`, guide wording): `git commit -am "chore: tune after end-to-end test"`.
