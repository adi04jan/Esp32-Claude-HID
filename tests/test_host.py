"""Pure-Python checks for the host side (no hardware). Run: python -m pytest tests -v"""
import sys
from pathlib import Path

import pytest
import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))
from hidlink import HidError, HidLink, char_to_key, parse_combo, to_abs  # noqa: E402


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
