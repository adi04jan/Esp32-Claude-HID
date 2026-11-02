"""MCP server: Claude's eyes (webcam) and hands (ESP32 BLE keyboard + mouse) on an offline PC.

Run by Claude Code via .mcp.json (stdio). Never print to stdout here: stdout is the MCP channel.
"""
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import cv2
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).parent))
from camera import CALIB, CONFIG, REC_DIR, Camera, CameraError  # noqa: E402
from hidlink import HidError, HidLink  # noqa: E402

GUIDE = """\
You are operating a REAL, separate, offline Windows PC. You see it only through a webcam pointed at its
monitor, and you act on it only through a Bluetooth keyboard and mouse (an ESP32). Nothing is installed on
that PC; it has no network. Its owner is usually sitting at it, working alongside you.

How to work:
- Take a screenshot before acting and after EVERY action; verify the result before the next step.
  Screens take a moment to update: if nothing changed, screenshot again before retrying.
- Coordinates for click/move/drag/scroll are pixels in the latest screenshot. Zoom images have their own
  scale (the zoom result tells you how to convert); never click using zoom-image coordinates.
- Camera images are slightly soft. Use zoom before reading code, error messages, small text or numbers.
- Prefer keyboard shortcuts over hunting with the mouse: win+r (Run), ctrl+l (address bar), alt+tab,
  ctrl+s, ctrl+f, win (Start menu, then type to search), alt+f4 only when sure.
- The owner may be using the PC too. Look before acting, never fight them for focus, and if the screen
  changes unexpectedly, stop and ask in chat.
- If a tool says "paused", the owner pressed the kill switch. Stop, tell them, and wait; don't retry.
- Typing: US layout, ASCII only (no smart quotes, em dashes or emoji). Send long text in chunks of
  ~1000 characters and check each landed. Type passwords or secrets only when the owner asks you to.
- Code editors auto-indent and auto-close brackets/quotes, which mangles typed code. Type code into
  Notepad and copy it over, or ask the owner before turning off VS Code's editor.autoIndent,
  editor.autoClosingBrackets, editor.autoClosingQuotes and editor.formatOnType.
- Spreadsheets/slides: prefer keyboard navigation (ctrl+arrows, F2 to edit a cell, tab/enter to move).
- If status shows "not calibrated", ask the owner to run the camera calibration (see README).
"""

mcp = MCPServer("desktop-hid", instructions=GUIDE)
hid: HidLink | None = None
cam: Camera | None = None


@contextmanager
def errors():
    """mcp 2.x hides unexpected exceptions from the agent; turn ours into readable tool errors."""
    try:
        yield
    except (HidError, CameraError, ValueError) as e:
        raise ToolError(str(e)) from e


def shot_to_frac(x: int, y: int, size: tuple[int, int]) -> tuple[float, float]:
    W, H = size
    if not (0 <= x < W and 0 <= y < H):
        raise ValueError(f"x,y must be inside the screenshot: 0..{W - 1}, 0..{H - 1} "
                         "(zoom coordinates are not click coordinates)")
    return x / (W - 1), y / (H - 1)


def _frac(x: int, y: int) -> tuple[float, float]:
    cam.require_calibrated()  # an uncalibrated view can't be mapped to the screen: send nothing
    return shot_to_frac(x, y, cam.size)


def _jpeg(img) -> Image:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise CameraError("JPEG encoding failed")
    return Image(data=buf.tobytes(), format="jpeg")


@mcp.tool()
def screenshot() -> list:
    """Photo of the offline PC's whole screen, flattened to a front-on view. Click coordinates are pixels in it."""
    with errors():
        img = cam.screenshot()
        W, H = cam.size
        note = "" if cam.calibrated else " (NOT calibrated: raw camera view; mouse tools are disabled)"
        return [f"screen {W}x{H}{note}; click coordinates are pixels in this image", _jpeg(img)]


@mcp.tool()
def zoom(x: int, y: int, width: int, height: int) -> list:
    """Magnified view of a screenshot region (x, y = its top-left, in screenshot pixels) for reading small text."""
    with errors():
        img, s = cam.zoom(x, y, width, height)
        return [f"zoom x{s:.2f} of ({x},{y},{width},{height}); a point (zx,zy) in this image is "
                f"({x}+zx/{s:.2f}, {y}+zy/{s:.2f}) on the screenshot", _jpeg(img)]


@mcp.tool()
def click(x: int, y: int, button: Literal["left", "right", "middle"] = "left", double: bool = False) -> str:
    """Click at screenshot pixel (x, y)."""
    with errors():
        hid.click(*_frac(x, y), button=button, double=double)
        return "ok"


@mcp.tool()
def move(x: int, y: int) -> str:
    """Move the pointer to screenshot pixel (x, y) without clicking (hover)."""
    with errors():
        hid.move(*_frac(x, y))
        return "ok"


@mcp.tool()
def drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """Left-button drag from (x1, y1) to (x2, y2), in screenshot pixels."""
    with errors():
        hid.drag(*_frac(x1, y1), *_frac(x2, y2))
        return "ok"


@mcp.tool()
def scroll(x: int, y: int, direction: Literal["up", "down"], clicks: int = 3) -> str:
    """Scroll the mouse wheel over screenshot pixel (x, y). One click is about 3 lines."""
    with errors():
        hid.scroll(*_frac(x, y), clicks if direction == "up" else -clicks)
        return "ok"


@mcp.tool()
def type_text(text: str) -> str:
    """Type ASCII text on the offline PC (US layout). Newlines press Enter, tabs press Tab."""
    with errors():
        hid.type_text(text)
        return f"typed {len(text)} characters"


@mcp.tool()
def key(combo: str, repeat: int = 1) -> str:
    """Press a key or combo, e.g. "enter", "ctrl+s", "ctrl+shift+esc", "win+r", "alt+tab", "f5", "down"."""
    with errors():
        hid.key(combo, repeat)
        return "ok"


@mcp.tool()
def status() -> str:
    """Keyboard/mouse link, kill switch, camera and recording state."""
    try:
        hid_line = "hid: " + " ".join(f"{k}={v}" for k, v in hid.status().items())
    except HidError as e:
        hid_line = f"hid: {e}"
    W, H = cam.size
    cam_line = f"camera: index={cam.index} calibrated={cam.calibrated} screenshot={W}x{H}"
    if cam.error:
        cam_line += f" error={cam.error}"
    return "\n".join([hid_line, cam_line, f"recording: {cam.recording or 'none'}"])


def main() -> None:
    global hid, cam
    cfg = json.loads(CONFIG.read_text())
    hid = HidLink(cfg["port"], cfg["key_delay_ms"] / 1000)  # port opens lazily on first command
    cam = Camera(cfg, CALIB, REC_DIR)
    try:
        cam.start()
    except CameraError:
        pass  # tools report cam.error; the keyboard still works
    try:
        mcp.run()
    finally:
        cam.stop()
        hid.close()


if __name__ == "__main__":
    main()
