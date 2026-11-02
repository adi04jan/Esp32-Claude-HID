"""Pure-Python checks for the host side (no hardware). Run: python -m pytest tests -v"""
import asyncio
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
import serial
from mcp.server.mcpserver.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))
from camera import Camera, CameraError, screen_matrix, shot_size, zoom_matrix  # noqa: E402
from hidlink import HidError, HidLink, char_to_key, parse_combo, to_abs  # noqa: E402
import server  # noqa: E402
from server import shot_to_frac  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent / "host" / "config.json").read_text())


class FakeSerial:
    def __init__(self, replies=None, fail_first_write=False):
        self.replies = list(replies or [])
        self.fail = fail_first_write
        self.lines = []

    def write(self, b):
        if self.fail:
            self.fail = False
            raise serial.SerialException("device unplugged")
        self.lines.append(b.decode().rstrip("\n"))

    def readline(self):
        return self.replies.pop(0) if self.replies else b"OK\n"

    def close(self):
        pass


def test_char_to_key():
    assert char_to_key("a") == (0, 0x04) and char_to_key("A") == (0x02, 0x04)
    assert char_to_key("!") == (0x02, 0x1E) and char_to_key("0") == (0, 0x27)
    assert char_to_key("\n") == (0, 0x28) and char_to_key("~") == (0x02, 0x35)
    with pytest.raises(ValueError, match="é"):
        char_to_key("é")


def test_parse_combo():
    assert parse_combo("ctrl+shift+esc") == (0x03, 0x29)
    assert parse_combo("Win+R") == (0x08, 0x15)
    assert parse_combo("f5") == (0, 0x3E)
    assert parse_combo("Ctrl+A") == (0x01, 0x04)  # letters ignore case: Ctrl+A is select-all
    assert parse_combo("ctrl+?") == (0x03, 0x38)  # shifted symbols keep their shift
    assert parse_combo("win") == (0x08, 0)
    with pytest.raises(ValueError, match="foo"):
        parse_combo("ctrl+foo")


def test_to_abs():
    assert (to_abs(0), to_abs(1), to_abs(0.5)) == (0, 32767, 16384)
    with pytest.raises(ValueError):
        to_abs(1.01)


def test_type_text_crlf_is_one_enter():
    ser = FakeSerial()
    HidLink("X", 0, ser).type_text("a\r\nb")
    assert ser.lines == ["K 0 4 0 0 0 0 0", "K 0 0 0 0 0 0 0", "K 0 40 0 0 0 0 0",
                         "K 0 0 0 0 0 0 0", "K 0 5 0 0 0 0 0", "K 0 0 0 0 0 0 0"]


def test_non_ascii_rejected_before_typing():
    ser = FakeSerial()
    with pytest.raises(ValueError, match="“"):
        HidLink("X", 0, ser).type_text("ok “quote”")
    assert ser.lines == []


def test_kill_switch_mid_typing_stops_and_releases():
    ser = FakeSerial(replies=[b"OK\n", b"OK\n", b"ERR paused\n"])
    with pytest.raises(HidError, match="kill switch"):
        HidLink("X", 0, ser).type_text("abc")
    assert ser.lines == ["K 0 4 0 0 0 0 0", "K 0 0 0 0 0 0 0", "K 0 5 0 0 0 0 0", "R"]


def test_reconnect_once_on_serial_error():
    ser = FakeSerial(replies=[b"OK connected=1 paused=0 bonded=1\n"], fail_first_write=True)
    link = HidLink("X", 0, ser)
    link._open = lambda: ser
    assert link.status() == {"connected": True, "paused": False, "bonded": True}


def test_click_sends_absolute_position():
    ser = FakeSerial()
    HidLink("X", 0, ser).click(0.5, 1.0)
    assert ser.lines == ["M 0 16384 32767 0", "M 1 16384 32767 0", "M 0 16384 32767 0"]


def test_shot_size():
    assert shot_size(1280, (1920, 1080)) == (1280, 720)
    assert shot_size(1280, (1280, 1024)) == (1280, 1024)


def test_screen_matrix_maps_corners():
    c = [[100, 50], [900, 80], [880, 600], [120, 560]]
    out = cv2.perspectiveTransform(np.float32([c]), screen_matrix(c, (1280, 720)))[0]
    assert np.allclose(out, [[0, 0], [1279, 0], [1279, 719], [0, 719]], atol=1e-3)


def test_zoom_matrix_maps_region_to_output():
    c = [[0, 0], [1279, 0], [1279, 719], [0, 719]]
    M = screen_matrix(c, (1280, 720))
    Z = zoom_matrix(M, 100, 200, 320, 180, 4.0)
    out = cv2.perspectiveTransform(np.float32([[[100, 200], [420, 380]]]), Z)[0]
    assert np.allclose(out, [[0, 0], [1280, 720]], atol=1e-3)


def test_calibration_from_other_camera_size_is_rejected(tmp_path):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"camera": 1, "camera_size": [1280, 720], "screen": [1920, 1080],
                             "corners": [[0, 0], [1279, 0], [1279, 719], [0, 719]]}))
    cam = Camera(CFG, p, tmp_path)
    cam._frame = np.zeros((1080, 1920, 3), np.uint8)
    assert not cam.calibrated
    with pytest.raises(CameraError, match="calibrate"):
        cam.require_calibrated()


def test_shot_to_frac():
    assert shot_to_frac(0, 0, (1280, 720)) == (0.0, 0.0)
    assert shot_to_frac(1279, 719, (1280, 720)) == (1.0, 1.0)
    with pytest.raises(ValueError, match="inside the screenshot"):
        shot_to_frac(1280, 10, (1280, 720))


def test_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"screenshot", "zoom", "click", "move", "drag", "scroll", "type_text", "key", "status"}


def test_tool_errors_reach_the_agent():
    # mcp 2.x masks unexpected exceptions; our messages must arrive as ToolError text.
    server.hid = HidLink("X", 0, FakeSerial())
    with pytest.raises(ToolError, match="foo"):
        asyncio.run(server.mcp.call_tool("key", {"combo": "ctrl+foo"}))


def test_click_before_calibration_sends_nothing(tmp_path):
    ser = FakeSerial()
    server.hid = HidLink("X", 0, ser)
    server.cam = Camera(CFG, tmp_path / "none.json", tmp_path)
    server.cam._frame = np.zeros((1080, 1920, 3), np.uint8)
    with pytest.raises(ToolError, match="calibrate"):
        asyncio.run(server.mcp.call_tool("click", {"x": 10, "y": 10}))
    assert ser.lines == []
