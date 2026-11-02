"""Serial link to the ESP32 BLE HID bridge: US key map, key combos, absolute mouse.

CLI: python host/hidlink.py [--port COM31] status|forget|release|raw "<line>"
The port always comes from config/argument; never auto-detect (other ESP32s are attached).
"""
import argparse
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import serial

CONFIG = Path(__file__).with_name("config.json")
SHIFT = 0x02
MODS = {"ctrl": 0x01, "control": 0x01, "shift": 0x02, "alt": 0x04, "win": 0x08, "gui": 0x08, "meta": 0x08}
NAMED = {
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29, "backspace": 0x2A, "tab": 0x2B,
    "space": 0x2C, "capslock": 0x39, "printscreen": 0x46, "insert": 0x49, "home": 0x4A, "pageup": 0x4B,
    "delete": 0x4C, "del": 0x4C, "end": 0x4D, "pagedown": 0x4E, "right": 0x4F, "left": 0x50,
    "down": 0x51, "up": 0x52, "menu": 0x65, **{f"f{i}": 0x39 + i for i in range(1, 13)},
}
BUTTONS = {"left": 1, "right": 2, "middle": 4}
ERRORS = {
    "paused": "paused: the user pressed the kill switch on the ESP32. Stop and wait for them to resume; don't retry.",
    "not connected": "not connected: pair 'Claude HID' on the offline PC (or it's out of range).",
    "send failed": ("send failed: the Bluetooth link is congested or fading, so the last key or click may not have "
                    "registered. Take a screenshot and check before continuing."),
}
MAX_TYPE = 2000  # one call stays short enough to interrupt and check

# US layout: char -> (modifier, keycode)
KEYS = {"\n": (0, 0x28), "\t": (0, 0x2B), " ": (0, 0x2C)}
for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEYS[c], KEYS[c.upper()] = (0, 0x04 + i), (SHIFT, 0x04 + i)
for i, (c, s) in enumerate(zip("1234567890", "!@#$%^&*()")):
    KEYS[c], KEYS[s] = (0, 0x1E + i), (SHIFT, 0x1E + i)
for code, c, s in [(0x2D, "-", "_"), (0x2E, "=", "+"), (0x2F, "[", "{"), (0x30, "]", "}"), (0x31, "\\", "|"),
                   (0x33, ";", ":"), (0x34, "'", '"'), (0x35, "`", "~"), (0x36, ",", "<"), (0x37, ".", ">"),
                   (0x38, "/", "?")]:
    KEYS[c], KEYS[s] = (0, code), (SHIFT, code)


class HidError(RuntimeError):
    pass


def char_to_key(ch: str) -> tuple[int, int]:
    try:
        return KEYS[ch]
    except KeyError:
        raise ValueError(f"can't type {ch!r}: only ASCII on a US keyboard layout is supported") from None


def parse_combo(combo: str) -> tuple[int, int]:
    mod = key = 0
    for part in combo.split("+"):
        name = part.strip().lower()
        if name in MODS:
            mod |= MODS[name]
        elif name in NAMED:
            key = NAMED[name]
        elif len(name) == 1:
            m, key = char_to_key(name)  # letters lowercased: Ctrl+A is select-all, not Ctrl+Shift+A
            mod |= m
        else:
            raise ValueError(f"unknown key {part.strip()!r} in {combo!r}")
    return mod, key


def to_abs(frac: float) -> int:
    if not 0 <= frac <= 1:
        raise ValueError(f"screen fraction {frac} is outside 0..1")
    return round(frac * 32767)


class HidLink:
    def __init__(self, port: str, key_delay: float = 0.012, ser=None):
        self.port, self.key_delay, self._ser = port, key_delay, ser
        self._lock = threading.Lock()  # one serial command at a time
        self._action_lock = threading.Lock()  # one multi-report action at a time: no interleaving
        self.abort = threading.Event()  # set when the client cancels the tool call
        self._x = self._y = 0

    def _open(self):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, 115200, 2
        s.dtr = s.rts = False  # before open(): toggling them resets the ESP32-C3
        s.open()
        s.reset_input_buffer()
        return s

    def _drop(self):
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    def _send(self, line: str) -> str:
        if self._ser is None:
            self._ser = self._open()
        self._ser.write((line + "\n").encode())
        while True:
            r = self._ser.readline().decode(errors="replace").strip()
            if not r:
                raise HidError(f"no reply from the ESP32 on {self.port}")
            if r.startswith(("OK", "ERR")):
                return r

    def cmd(self, line: str) -> str:
        with self._lock:
            try:
                r = self._send(line)
            except (serial.SerialException, OSError):
                self._drop()  # unplugged/replugged: reopen once and retry (reports are state, so a resend is safe)
                try:
                    r = self._send(line)
                except (serial.SerialException, OSError) as e:
                    self._drop()
                    raise HidError(f"can't reach the ESP32 on {self.port}: {e}") from e
        if r.startswith("ERR"):
            reason = r[4:].strip()
            raise HidError(ERRORS.get(reason, f"ESP32 error: {reason}"))
        return r

    def status(self) -> dict[str, bool]:
        return {k: v == "1" for k, v in (kv.split("=") for kv in self.cmd("S").split()[1:])}

    def release(self) -> None:
        self.cmd("R")

    def forget(self) -> None:
        self.cmd("B")

    @contextmanager
    def _action(self):
        with self._action_lock:
            self.abort.clear()
            try:
                yield
            except HidError:
                try:
                    self.cmd("R")
                except Exception:
                    pass
                raise

    def _report(self, line: str) -> None:
        if self.abort.is_set():
            raise HidError("cancelled: the tool call was interrupted; input released")
        self.cmd(line)

    def _tap(self, mod: int, key: int) -> None:
        self._report(f"K {mod} {key} 0 0 0 0 0")
        time.sleep(self.key_delay)
        self._report("K 0 0 0 0 0 0 0")
        time.sleep(self.key_delay)

    def type_text(self, text: str) -> None:
        if len(text) > MAX_TYPE:
            raise ValueError(f"send at most {MAX_TYPE} characters per call; split longer text and check each part")
        keys = [char_to_key(c) for c in text.replace("\r\n", "\n")]  # validate everything before typing anything
        with self._action():
            done = 0
            try:
                for mod, key in keys:
                    self._tap(mod, key)
                    done += 1
            except HidError as e:
                raise HidError(f"{e} (typed {done} of {len(keys)} characters)") from e

    def key(self, combo: str, repeat: int = 1) -> None:
        if not 1 <= repeat <= 200:
            raise ValueError("repeat must be 1..200")
        mod, key = parse_combo(combo)
        with self._action():
            for _ in range(repeat):
                self._tap(mod, key)

    def _mouse(self, buttons: int, wheel: int = 0) -> None:
        self._report(f"M {buttons} {self._x} {self._y} {wheel}")

    def _goto(self, fx: float, fy: float) -> None:
        self._x, self._y = to_abs(fx), to_abs(fy)
        self._mouse(0)

    def move(self, fx: float, fy: float) -> None:
        to_abs(fx), to_abs(fy)
        with self._action():
            self._goto(fx, fy)

    def click(self, fx: float, fy: float, button: str = "left", double: bool = False) -> None:
        if button not in BUTTONS:
            raise ValueError(f"button must be one of {sorted(BUTTONS)}")
        to_abs(fx), to_abs(fy)
        with self._action():
            self._goto(fx, fy)
            for _ in range(2 if double else 1):
                self._mouse(BUTTONS[button])
                time.sleep(self.key_delay)
                self._mouse(0)
                time.sleep(self.key_delay)

    def drag(self, fx1: float, fy1: float, fx2: float, fy2: float, steps: int = 8) -> None:
        for f in (fx1, fy1, fx2, fy2):
            to_abs(f)
        with self._action():
            self._goto(fx1, fy1)
            self._mouse(1)
            for i in range(1, steps + 1):  # intermediate moves so Windows recognises a drag
                time.sleep(self.key_delay)
                self._x, self._y = to_abs(fx1 + (fx2 - fx1) * i / steps), to_abs(fy1 + (fy2 - fy1) * i / steps)
                self._mouse(1)
            time.sleep(self.key_delay)
            self._mouse(0)

    def scroll(self, fx: float, fy: float, clicks: int) -> None:
        if not -50 <= clicks <= 50:
            raise ValueError("clicks must be -50..50 (positive scrolls up)")
        to_abs(fx), to_abs(fy)
        with self._action():
            self._goto(fx, fy)
            for _ in range(abs(clicks)):
                self._mouse(0, 1 if clicks > 0 else -1)
                time.sleep(self.key_delay)

    def close(self) -> None:
        try:
            self.cmd("R")
        except Exception:
            pass
        self._drop()


def main() -> None:
    cfg = json.loads(CONFIG.read_text())
    ap = argparse.ArgumentParser(description="Talk to the Claude HID ESP32 bridge.")
    ap.add_argument("--port", default=cfg["port"])
    ap.add_argument("action", choices=["status", "forget", "release", "raw"])
    ap.add_argument("line", nargs="?", help="raw protocol line, e.g. \"M 0 16384 16384 0\"")
    a = ap.parse_args()
    link = HidLink(a.port)
    try:
        if a.action == "status":
            print(" ".join(f"{k}={v}" for k, v in link.status().items()))
        elif a.action == "forget":
            link.forget()
            print("Forgot the paired PC. Also remove 'Claude HID' in that PC's Bluetooth settings before re-pairing.")
        elif a.action == "release":
            link.release()
            print("released")
        else:
            print(link.cmd(a.line or ""))
    except HidError as e:
        raise SystemExit(str(e))
    finally:
        link.close()


if __name__ == "__main__":
    main()
