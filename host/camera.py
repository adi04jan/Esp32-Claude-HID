"""Webcam eyes: latest-frame capture, 4-corner calibration, rectified screenshots, zoom, recording.

CLI: python host/camera.py list
     python host/camera.py calibrate --camera N --screen 1920x1080
"""
import argparse
import os
import json
import threading
import time
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # quiet probing of absent camera indexes
import cv2
import numpy as np

HERE = Path(__file__).parent
CONFIG = HERE / "config.json"
CALIB = HERE / "calibration.json"
REC_DIR = HERE.parent / "recordings"
STALE_S = 2.0  # a frame older than this means the camera stopped; never serve it


class CameraError(RuntimeError):
    pass


def shot_size(shot_width: int, screen) -> tuple[int, int]:
    sw, sh = screen
    return shot_width, round(shot_width * sh / sw)


def screen_matrix(corners, size) -> np.ndarray:
    """Camera pixels -> screenshot pixels. Corners are the screen's TL, TR, BR, BL in the camera image."""
    W, H = size
    dst = np.float32([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]])
    return cv2.getPerspectiveTransform(np.float32(corners), dst)


def zoom_matrix(M, x, y, w, h, scale) -> np.ndarray:
    """Camera pixels -> zoomed image of screenshot region (x, y, w, h), magnified by scale."""
    S = np.array([[scale, 0, 0], [0, scale, 0], [0, 0, 1]], dtype=float)
    T = np.array([[1, 0, -x], [0, 1, -y], [0, 0, 1]], dtype=float)
    return S @ T @ M


def load_calibration(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def open_capture(index: int, width: int, height: int):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise CameraError(f"can't open camera {index} (wrong index, unplugged, or in use by another program)")
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # before size: YUY2 1080p is ~5 fps on USB2
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


class Camera:
    def __init__(self, cfg: dict, calib_path: Path, rec_dir: Path):
        self.cfg, self.calib_path, self.rec_dir = cfg, Path(calib_path), Path(rec_dir)
        self.calib = load_calibration(self.calib_path)
        self.index = self.calib["camera"] if self.calib else 0
        self._M = screen_matrix(self.calib["corners"], self._calib_size()) if self.calib else None
        self._frame, self._frame_t = None, 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = self._cap = self._writer = None
        self.recording: Path | None = None
        self.error: str | None = None

    def _calib_size(self):
        return shot_size(self.cfg["shot_width"], self.calib["screen"])

    def _calibrated_for(self, f) -> bool:
        return bool(self.calib) and f is not None and list(self.calib["camera_size"]) == [f.shape[1], f.shape[0]]

    @property
    def calibrated(self) -> bool:
        return self._calibrated_for(self._frame)

    @property
    def size(self) -> tuple[int, int]:
        if self.calibrated:
            return self._calib_size()
        f = self._frame
        return shot_size(self.cfg["shot_width"], (f.shape[1], f.shape[0]) if f is not None else (16, 9))

    def require_calibrated(self) -> None:
        if not self.calibrated:
            raise CameraError(
                "camera not calibrated for this setup — run: python host/camera.py calibrate --camera N --screen WxH "
                "(stop the Claude session first; the camera can only be opened by one program)")

    def _matrix(self, f) -> np.ndarray:
        if self._calibrated_for(f):
            return self._M
        W, H = self.size
        return np.diag([W / f.shape[1], H / f.shape[0], 1.0])  # uncalibrated: plain resize of the raw frame

    def latest(self) -> np.ndarray:
        if self._thread is None:
            raise CameraError(self.error or "camera not started")
        deadline = time.time() + 3
        while time.time() < deadline:
            with self._lock:
                f, t = self._frame, self._frame_t
            if f is not None and time.time() - t < STALE_S:
                return f
            time.sleep(0.05)
        raise CameraError(f"no fresh frame from camera {self.index}{': ' + self.error if self.error else ''}")

    def screenshot(self) -> np.ndarray:
        f = self.latest()
        return cv2.warpPerspective(f, self._matrix(f), self.size, flags=cv2.INTER_AREA)

    def zoom(self, x: int, y: int, w: int, h: int) -> tuple[np.ndarray, float]:
        W, H = self.size
        if not (w >= 8 and h >= 8 and x >= 0 and y >= 0 and x + w <= W and y + h <= H):
            raise ValueError(f"zoom region must lie inside the {W}x{H} screenshot, width and height >= 8")
        f = self.latest()
        s = 1280 / max(w, h)
        Z = zoom_matrix(self._matrix(f), x, y, w, h, s)
        return cv2.warpPerspective(f, Z, (round(w * s), round(h * s)), flags=cv2.INTER_CUBIC), s

    def start(self) -> None:
        w, h = self.calib["camera_size"] if self.calib else (self.cfg["camera_width"], self.cfg["camera_height"])
        try:
            self._cap = open_capture(self.index, w, h)
        except CameraError as e:
            self.error = str(e)
            raise
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        fps, next_rec = self.cfg["record_fps"], 0.0
        while not self._stop.is_set():
            ok, f = self._cap.read()
            if not ok:
                self.error = "camera read failed"
                time.sleep(0.1)
                continue
            now = time.time()
            with self._lock:
                self._frame, self._frame_t = f, now
            if now >= next_rec:
                next_rec = now + 1 / fps
                try:
                    self._record(f, now)
                except Exception as e:  # recording must never stop the eyes; surface it via status
                    self.error = f"recording failed: {e}"
        self._close_writer()
        self._cap.release()

    def _record(self, f, now: float) -> None:
        img = cv2.warpPerspective(f, self._matrix(f), self.size, flags=cv2.INTER_AREA)
        size = (img.shape[1], img.shape[0])
        seg = self.cfg["record_segment_min"] * 60
        # New file per segment so a killed process loses at most one segment's footer.
        if self._writer is None or now - self._seg_start >= seg or size != self._wsize:
            self._close_writer()
            self.rec_dir.mkdir(parents=True, exist_ok=True)
            path = self.rec_dir / time.strftime("%Y%m%d-%H%M%S.mp4")
            self._writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), self.cfg["record_fps"], size)
            self._wsize, self._seg_start, self.recording = size, now, path
        self._writer.write(img)

    def _close_writer(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None


def cmd_list(cfg: dict) -> None:
    for i in range(5):
        try:
            cap = open_capture(i, cfg["camera_width"], cfg["camera_height"])
        except CameraError:
            continue
        ok, f = cap.read()
        print(f"camera {i}: " + (f"{f.shape[1]}x{f.shape[0]}" if ok else "opens but gives no frames"))
        cap.release()


def cmd_calibrate(cfg: dict, index: int, screen: tuple[int, int]) -> None:
    cap = open_capture(index, cfg["camera_width"], cfg["camera_height"])
    ok, f = cap.read()
    if not ok:
        raise SystemExit(f"camera {index} gives no frames")
    fh, fw = f.shape[:2]
    scale = min(1.0, 1280 / fw)
    corners: list[list[float]] = []
    win = "Click screen corners TL, TR, BR, BL | r = reset, Enter = save, Esc = cancel"

    def on_mouse(ev, x, y, *_):
        if ev == cv2.EVENT_LBUTTONDOWN and len(corners) < 4:
            corners.append([x / scale, y / scale])

    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    while True:
        ok, f = cap.read()
        if ok:
            view = cv2.resize(f, None, fx=scale, fy=scale)
            pts = [(round(x * scale), round(y * scale)) for x, y in corners]
            for i, p in enumerate(pts):
                cv2.circle(view, p, 6, (0, 0, 255), -1)
                cv2.putText(view, "TL TR BR BL".split()[i], (p[0] + 8, p[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            if len(pts) > 1:
                cv2.polylines(view, [np.int32(pts)], len(pts) == 4, (0, 255, 0), 2)
            cv2.imshow(win, view)
        k = cv2.waitKey(30) & 0xFF
        if k == 27 or cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
            print("cancelled; calibration unchanged")
            return
        if k == ord("r"):
            corners.clear()
        if k in (10, 13) and len(corners) == 4:
            break
    CALIB.write_text(json.dumps({"camera": index, "camera_size": [fw, fh], "screen": list(screen), "corners": corners}, indent=2))
    print(f"saved {CALIB}")
    M = screen_matrix(corners, shot_size(cfg["shot_width"], screen))
    cv2.destroyAllWindows()
    ok, f = cap.read()
    if ok:
        cv2.imshow("Rectified preview: press any key", cv2.warpPerspective(f, M, shot_size(cfg["shot_width"], screen)))
        cv2.waitKey(0)
    cap.release()
    cv2.destroyAllWindows()


def main() -> None:
    cfg = json.loads(CONFIG.read_text())
    ap = argparse.ArgumentParser(description="Camera tools for the Claude HID setup.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show which camera indexes open")
    c = sub.add_parser("calibrate", help="click the offline screen's corners")
    c.add_argument("--camera", type=int, required=True)
    c.add_argument("--screen", required=True, help="offline PC resolution, e.g. 1920x1080")
    a = ap.parse_args()
    if a.cmd == "list":
        cmd_list(cfg)
    else:
        sw, sh = (int(v) for v in a.screen.lower().split("x"))
        cmd_calibrate(cfg, a.camera, (sw, sh))


if __name__ == "__main__":
    main()
