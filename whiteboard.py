#!/usr/bin/env python3
"""
whiteboard.py — Scan a whiteboard with your phone camera.

The phone streams its camera over Wi-Fi (IP Webcam or DroidCam app);
this script grabs the stream on the PC, rectifies the board, cleans the
image, erases whatever moves in front of it (you), and automatically
saves each new state of the board.

Usage:
    python whiteboard.py http://PHONE_IP:8080/video --size 120x90

Keys (in the window):
    s         save now
    v         switch view: stabilized board / live cleaned / raw camera
    c         recalibrate (click the 4 corners again)
    r         reset the stabilized board
    q, Esc    quit (writes a PDF of the session if Pillow is installed)
"""

import argparse
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# dropped network stream -> read() gives up after 5 s instead of blocking
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rw_timeout;5000000")

import cv2
import numpy as np

cv2.setNumThreads(2)  # keeps OpenCV from grabbing every core for nothing

CALIBRATION_FILE = Path("calibration.json")
WINDOW = "Whiteboard"


# ---------------------------------------------------------------------------
# Stream reading in a thread (avoids accumulating lag)
# ---------------------------------------------------------------------------
class Stream:
    def __init__(self, source, rate=12):
        self.source = source
        self.interval = 1.0 / rate  # only ~12 frames/s are decoded to color
        self.cap = cv2.VideoCapture(source)
        if not self.cap.isOpened():
            raise SystemExit(f"Cannot open stream: {source}\n"
                             "-> Check the address in a browser and that the PC "
                             "and the phone are on the same Wi-Fi.")
        self.frame = None
        self.t_frame = 0.0
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        failures = 0
        last = 0.0
        while self.running:
            # grab() drains the stream (no accumulating lag); retrieve()
            # (color conversion, costly at 1080p) only when needed
            ok = self.cap.grab()
            f = None
            if ok and time.time() - last >= self.interval:
                ok, f = self.cap.retrieve()
                last = time.time()
            elif ok:
                failures = 0
                continue
            if not ok:
                failures += 1
                if failures >= 40:  # ~2 s without a frame -> reconnect
                    self.cap.release()
                    time.sleep(1)
                    self.cap = cv2.VideoCapture(self.source)
                    failures = 0
                time.sleep(0.05)
                continue
            failures = 0
            with self.lock:
                self.frame = f
                self.t_frame = time.time()

    def read(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def age(self):
        """Seconds since the last frame was received."""
        return time.time() - self.t_frame

    def wait_frame(self, timeout=10.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            f = self.read()
            if f is not None:
                return f
            time.sleep(0.05)
        raise SystemExit("No frame received from the phone (timeout).")

    def stop(self):
        self.running = False
        self.thread.join(timeout=1)
        self.cap.release()


# ---------------------------------------------------------------------------
# Calibration: click the 4 corners of the board
# ---------------------------------------------------------------------------
def order_corners(pts):
    """Return the corners as top-left, top-right, bottom-right, bottom-left."""
    pts = np.array(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    d = (pts[:, 1] - pts[:, 0])
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def calibrate(stream, cancellable=False):
    """Click the 4 corners. Returns them, or None if cancelled (q/Esc) when
    `cancellable` (recalibration: the previous corners are kept)."""
    pts = []

    def click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(pts) < 4:
            pts.append((x, y))

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW, click)
    shape = None
    while True:
        f = stream.read()
        if f is None:
            cv2.waitKey(30)
            continue
        shape = f.shape[:2]
        disp = f.copy()
        for p in pts:
            cv2.circle(disp, p, 10, (0, 0, 255), -1)
        if len(pts) >= 2:
            cv2.polylines(disp, [np.array(pts, np.int32)], len(pts) == 4, (0, 0, 255), 3)
        msg = (f"Click the 4 corners of the board ({len(pts)}/4) - u: undo"
               if len(pts) < 4 else "Enter/Space: confirm - u: undo")
        if cancellable:
            msg += " - q: cancel"
        cv2.putText(disp, msg, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 5)
        cv2.putText(disp, msg, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
        cv2.imshow(WINDOW, disp)
        k = cv2.waitKey(30) & 0xFF
        if k == ord("u") and pts:
            pts.pop()
        elif k in (13, 32) and len(pts) == 4:
            break
        elif k in (ord("q"), 27):
            cv2.setMouseCallback(WINDOW, lambda *a: None)
            if cancellable:
                print("Recalibration cancelled, previous corners kept.")
                return None
            raise SystemExit("Calibration cancelled.")
    cv2.setMouseCallback(WINDOW, lambda *a: None)
    corners = order_corners(pts)
    CALIBRATION_FILE.write_text(json.dumps({"corners": corners.tolist(), "shape": list(shape)}))
    return corners


def load_calibration(shape):
    if not CALIBRATION_FILE.exists():
        return None
    data = json.loads(CALIBRATION_FILE.read_text())
    if list(data.get("shape", [])) != list(shape):
        return None  # different resolution -> recalibrate
    return np.array(data["corners"], dtype=np.float32)


# ---------------------------------------------------------------------------
# Cleaning: uniform white background, high-contrast ink, colors kept
# ---------------------------------------------------------------------------
def clean(img, gamma=2.2, white_threshold=0.88):
    h, w = img.shape[:2]
    # Background (lighting) estimate: downscale, "dilate" to erase thin
    # strokes, smooth, scale back up.
    small = cv2.resize(img, (w // 4, h // 4), interpolation=cv2.INTER_AREA)
    k = max(5, (w // 160) | 1)
    small = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    small = cv2.GaussianBlur(small, (0, 0), k)
    background = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)

    # image/background ratio (1 = white background), in uint8 for speed
    ratio = cv2.divide(img, background, scale=255)
    # darken the ink (gamma curve) while keeping its hue
    lut = (255.0 * (np.arange(256) / 255.0) ** gamma).astype(np.uint8)
    out = cv2.LUT(ratio, lut)
    # anything almost white on all 3 channels -> pure white
    lowest = cv2.min(cv2.min(ratio[..., 0], ratio[..., 1]), ratio[..., 2])
    white = cv2.compare(lowest, int(white_threshold * 255), cv2.CMP_GT)
    return cv2.max(out, cv2.merge([white, white, white]))


# ---------------------------------------------------------------------------
# Stabilized board: only still areas are updated
# (whatever moves = your hand / your body -> ignored)
# ---------------------------------------------------------------------------
class StableBoard:
    """
    Motion is measured on the RAW rectified image (cleaning turns a person
    almost white, hence "still" -> they would get baked into the board).
    A cell is copied only if it has not moved for n_frames samples. A cell
    where a large part changed compared to the stored board (an object, a
    person standing still — not just ink strokes) must also stay still for
    n_object more samples.
    A large blob with ANY moving part (the writing arm) is blocked ENTIRELY,
    even if the rest of it (back, torso) has been still for a long time.
    """

    def __init__(self, n_frames=12, grid=(32, 20), pixel_threshold=25,
                 motion_fraction=0.01, blob_threshold=20, object_threshold=30,
                 object_fraction=0.15, n_object=40):
        self.buffer = deque(maxlen=n_frames)
        self.grid = grid                          # (columns, rows)
        self.pixel_threshold = pixel_threshold    # gray difference = "it moved"
        self.motion_fraction = motion_fraction    # fraction of moved pixels -> unstable cell
        self.blob_threshold = blob_threshold      # difference to stored board (blocking)
        self.object_threshold = object_threshold  # difference to stored board (object)
        self.object_fraction = object_fraction    # fraction of large changed blob -> "object" cell
        self.object_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
        self.n_object = n_object                  # extra still samples (40 = 10 s)
        self.board = None
        self.board_raw = None                     # same, as small raw grayscale
        self.still_count = np.zeros((grid[1], grid[0]), np.int32)
        self.last_change = time.time()

    def reset(self):
        self.buffer.clear()
        self.board = None
        self.board_raw = None
        self.still_count[:] = 0

    @staticmethod
    def _small_gray(raw):
        h, w = raw.shape[:2]
        g = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (w // 4, h // 4), interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(g, (5, 5), 0)

    def update(self, cleaned, raw):
        g = self._small_gray(raw)
        self.buffer.append(g)
        if self.board is None:
            self.board = cleaned.copy()
            self.board_raw = g.copy()
            self.last_change = time.time()
            return
        if len(self.buffer) < self.buffer.maxlen:
            return

        stack = np.stack(self.buffer)
        moved = ((stack.max(axis=0) - stack.min(axis=0)) > self.pixel_threshold).astype(np.float32)
        gx, gy = self.grid
        fraction = cv2.resize(moved, (gx, gy), interpolation=cv2.INTER_AREA)
        unstable = (fraction > self.motion_fraction).astype(np.uint8)
        unstable = cv2.dilate(unstable, np.ones((3, 3), np.uint8))  # margin around motion
        if unstable.mean() > 0.6:  # almost everything moves: light, exposure, shake
            self.still_count[:] = 0
            return
        self.still_count = np.where(unstable > 0, 0, self.still_count + 1)

        H, W = cleaned.shape[:2]
        h, w = g.shape
        ys = np.linspace(0, H, gy + 1).astype(int)
        xs = np.linspace(0, W, gx + 1).astype(int)
        ys_s = np.linspace(0, h, gy + 1).astype(int)
        xs_s = np.linspace(0, w, gx + 1).astype(int)
        # "object" cells: large changed blobs (the opening erases thin ink
        # strokes, keeps people) + a one-cell margin
        changed = (cv2.absdiff(g, self.board_raw) > self.object_threshold).astype(np.uint8)
        changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, self.object_kernel)
        objects = (cv2.resize(changed.astype(np.float32), (gx, gy),
                              interpolation=cv2.INTER_AREA) > self.object_fraction)
        objects = cv2.dilate(objects.astype(np.uint8), np.ones((3, 3), np.uint8))
        # everything that changed (low threshold, no opening: light and
        # striped clothes included) and touches motion -> blocked entirely
        blob = (cv2.absdiff(g, self.board_raw) > self.blob_threshold).astype(np.uint8)
        n, labels = cv2.connectedComponents(blob, connectivity=8)
        blocked = np.zeros((gy, gx), np.uint8)
        if n > 1:
            motion = cv2.dilate(moved.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
            touched = np.unique(labels[motion & (labels > 0)])
            if touched.size:
                px = np.isin(labels, touched).astype(np.float32)
                blocked = (cv2.resize(px, (gx, gy), interpolation=cv2.INTER_AREA) > 0).astype(np.uint8)
                blocked = cv2.dilate(blocked, np.ones((3, 3), np.uint8))
                self.still_count[blocked > 0] = 0  # the countdown starts over
        changed_any = False
        for j in range(gy):
            for i in range(gx):
                if unstable[j, i] or blocked[j, i]:
                    continue
                y0, y1, x0, x1 = ys[j], ys[j + 1], xs[i], xs[i + 1]
                new = cleaned[y0:y1, x0:x1]
                if cv2.absdiff(new, self.board[y0:y1, x0:x1]).mean() <= 1.5:
                    continue
                if objects[j, i] and self.still_count[j, i] < self.n_object:
                    continue
                self.board[y0:y1, x0:x1] = new
                sy0, sy1, sx0, sx1 = ys_s[j], ys_s[j + 1], xs_s[i], xs_s[i + 1]
                self.board_raw[sy0:sy1, sx0:sx1] = g[sy0:sy1, sx0:sx1]
                changed_any = True
        if changed_any:
            self.last_change = time.time()


# ---------------------------------------------------------------------------
# Camera monitoring: black frame, phone that moved
# ---------------------------------------------------------------------------
class CameraMonitor:
    """
    Compares the full raw frame (including the room around the board, which
    is well textured) with a reference taken at calibration, using phase
    correlation. A clear and lasting shift = the phone probably moved:
    warning only (the board is frozen only with --freeze-if-moved).
    The warning clears by itself when the shift is no longer measured.
    """

    def __init__(self, width=320, shift_threshold=0.012, n_confirm=4):
        self.width = width
        self.threshold = shift_threshold * width  # in pixels of the small image
        self.n_confirm = n_confirm                # consecutive samples (4 = 1 s)
        self.ref = None
        self.window = None
        self.shifts = deque(maxlen=n_confirm)     # latest shifts (dx, dy)
        self.moved = False

    def _small(self, f):
        h, w = f.shape[:2]
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (self.width, int(h * self.width / w)), interpolation=cv2.INTER_AREA)
        return np.float32(g)

    def reset(self, f=None):
        self.ref = None if f is None else self._small(f)
        self.shifts.clear()
        self.moved = False

    def check(self, f):
        """Return 'ok', 'black' or 'moved'."""
        g = self._small(f)
        if g.mean() < 15:
            return "black"
        if self.ref is None or self.ref.shape != g.shape:
            self.ref = g
            self.window = cv2.createHanningWindow(g.shape[::-1], cv2.CV_32F)
            return "ok"
        if self.window is None or self.window.shape != g.shape:
            self.window = cv2.createHanningWindow(g.shape[::-1], cv2.CV_32F)
        (dx, dy), _ = cv2.phaseCorrelate(self.ref, g, self.window)
        self.shifts.append((dx, dy))
        if len(self.shifts) < self.n_confirm:
            return "moved" if self.moved else "ok"
        m = np.array(self.shifts)
        norms = np.hypot(m[:, 0], m[:, 1])
        if not self.moved:
            # dark room = noisy, random measurements; a real move gives
            # the SAME shift several times in a row
            spread = np.hypot(*(m - np.median(m, axis=0)).T).max()
            if (norms > self.threshold).all() and spread < self.threshold / 2:
                self.moved = True
                mx, my = np.median(m, axis=0)
                print(f"[warning] phone moved? shift {mx:+.0f},{my:+.0f} px "
                      "(c to recalibrate)")
        elif (norms <= self.threshold).all():
            self.moved = False
            print("[warning] shift is gone")
        return "moved" if self.moved else "ok"


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------
class Captures:
    """
    Manual captures (s key): all kept.
    Automatic captures: only the `max_auto` most recent ones (safety net);
    the oldest one is deleted when a new one arrives.
    """

    def __init__(self, root, max_auto=3):
        self.folder = Path(root) / datetime.now().strftime("%Y-%m-%d_%Hh%M")
        self.max_auto = max_auto
        self.files = []        # (path, auto?) in chronological order
        self.number = 0
        self.reference = None  # last saved state (grayscale)

    def save(self, img, auto=False):
        self.folder.mkdir(parents=True, exist_ok=True)  # no empty folders
        self.number += 1
        kind = "auto" if auto else "manual"
        name = self.folder / f"{self.number:03d}_{datetime.now():%H%M%S}_{kind}.png"
        cv2.imwrite(str(name), img)
        self.files.append((name, auto))
        self.reference = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        print(f"[saved {kind}] {name}")
        autos = [f for f, a in self.files if a]
        while auto and len(autos) > self.max_auto:
            oldest = autos.pop(0)
            oldest.unlink(missing_ok=True)
            self.files.remove((oldest, True))

    def has_changed(self, img, min_fraction=0.002):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if (gray < 200).mean() < 0.0005:  # empty board -> nothing to keep
            return False
        if self.reference is None:
            return True
        return (cv2.absdiff(gray, self.reference) > 60).mean() > min_fraction

    def export_pdf(self):
        if not self.files:
            return
        try:
            from PIL import Image
        except ImportError:
            print("Pillow missing: no PDF (pip install pillow).")
            return
        pages = [Image.open(f).convert("RGB") for f, _ in self.files]
        pdf = self.folder / "session.pdf"
        pages[0].save(pdf, save_all=True, append_images=pages[1:])
        print(f"[pdf] {pdf}")


# ---------------------------------------------------------------------------
def board_size(text):
    """argparse type for --size: 'WxH' in cm, e.g. 120x90."""
    try:
        w, h = (float(x) for x in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected WxH in cm (e.g. 120x90), got {text!r}")
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("width and height must be positive")
    return w, h


def non_negative_int(text):
    n = int(text)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def main():
    ap = argparse.ArgumentParser(description="Whiteboard scanner using a phone camera")
    ap.add_argument("source", help="stream URL (e.g. http://PHONE_IP:8080/video) "
                                   "or webcam number (0, 1...)")
    ap.add_argument("--size", type=board_size, default="120x90",
                    help="board dimensions in cm, WxH (default 120x90)")
    ap.add_argument("--width", type=int, default=1600,
                    help="width of the rectified image in pixels (default 1600)")
    ap.add_argument("--recalibrate", action="store_true", help="ignore the saved calibration")
    ap.add_argument("--dir", default="captures", help="captures folder")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds of calm before an automatic capture (default 3)")
    ap.add_argument("--freeze-if-moved", action="store_true",
                    help="freeze the board when the phone seems to have moved "
                         "(default: warning only)")
    ap.add_argument("--max-auto", type=non_negative_int, default=3,
                    help="automatic captures kept (most recent, default 3); "
                         "manual captures (s) are all kept")
    ap.add_argument("--fps", type=float, default=10,
                    help="displayed frames/s (default 10; lower = cooler PC)")
    args = ap.parse_args()

    source = int(args.source) if args.source.isdigit() else args.source
    width_cm, height_cm = args.size
    W = args.width
    H = int(round(W * height_cm / width_cm))
    target = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], dtype=np.float32)

    stream = Stream(source)
    first = stream.wait_frame()
    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)

    corners = None if args.recalibrate else load_calibration(first.shape[:2])
    if corners is None:
        corners = calibrate(stream)
    M = cv2.getPerspectiveTransform(corners, target)

    board = StableBoard()
    monitor = CameraMonitor()
    monitor.reset(stream.read())
    captures = Captures(args.dir, args.max_auto)
    views = ["board", "live", "raw"]
    view = 0
    last_sample = 0.0
    state = "ok"
    active = True
    warnings = {"black": "BLACK FRAME - camera covered?",
                "moved": "Phone moved? - c: recalibrate",
                "stream": "STREAM LOST - reconnecting..."}
    print(f"Captures in: {captures.folder} (s = capture; last {args.max_auto} auto kept)")

    last_display = 0.0
    try:
        while True:
            k = cv2.waitKey(15) & 0xFF  # yields the CPU between iterations
            f = stream.read()
            if f is None:
                continue
            now = time.time()

            # Heavy processing: only 4 times per second
            if now - last_sample >= 0.25:
                last_sample = now
                state = "stream" if stream.age() > 2 else monitor.check(f)
                active = state == "ok" or (state == "moved" and not args.freeze_if_moved)
                if active:  # otherwise the board is frozen
                    rectified = cv2.warpPerspective(f, M, (W, H))
                    cleaned = clean(rectified)
                    board.update(cleaned, rectified)
                # Automatic capture: the board changed, then stayed calm
                if (active and board.board is not None
                        and now - board.last_change > args.delay
                        and captures.has_changed(board.board)):
                    captures.save(board.board, auto=True)

            # Keys
            if k in (ord("q"), 27):
                break
            elif k == ord("s") and board.board is not None:
                captures.save(board.board)
            elif k == ord("v"):
                view = (view + 1) % len(views)
            elif k == ord("r"):
                board.reset()
                monitor.reset(f)
            elif k == ord("c"):
                new_corners = calibrate(stream, cancellable=True)
                if new_corners is not None:
                    corners = new_corners
                    M = cv2.getPerspectiveTransform(corners, target)
                    board.reset()
                    monitor.reset(stream.read())

            # Display: capped at --fps frames/s (immediate after a key press)
            if now - last_display < 1.0 / args.fps and k == 0xFF:
                continue
            last_display = now
            if views[view] == "board" and board.board is not None:
                disp = board.board.copy()
            elif views[view] == "live":
                disp = clean(cv2.warpPerspective(f, M, (W, H)))
            else:
                disp = f.copy()
                cv2.polylines(disp, [corners.astype(np.int32)], True, (0, 0, 255), 3)
            n_manual = sum(not a for _, a in captures.files)
            info = (f"view: {views[view]} | captures: {n_manual} (s) + "
                    f"{len(captures.files) - n_manual} auto | s v c r q")
            cv2.putText(disp, info, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 5)
            cv2.putText(disp, info, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 60, 0), 2)
            if state != "ok":
                cv2.putText(disp, warnings[state], (15, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 6)
                cv2.putText(disp, warnings[state], (15, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 230), 2)
            cv2.imshow(WINDOW, disp)
    finally:
        stream.stop()
        cv2.destroyAllWindows()
        captures.export_pdf()


if __name__ == "__main__":
    main()
