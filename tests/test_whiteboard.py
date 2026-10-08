"""
Tests on simulated frames (~1 min):

    python tests/test_whiteboard.py

Must end with "ALL OK".
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import whiteboard as wb

rng = np.random.default_rng(0)

# simulated "room": textured background with a whiteboard in the middle
room = cv2.GaussianBlur(rng.integers(40, 200, (1080, 1920, 3), dtype=np.uint8), (0, 0), 6)
room[150:930, 360:1560] = (225, 228, 230)
cv2.putText(room, "x^2+1", (500, 500), cv2.FONT_HERSHEY_SIMPLEX, 2, (30, 30, 30), 4)
corners = np.float32([[360, 150], [1559, 150], [1559, 929], [360, 929]])
W, H = 1600, 1200
M = cv2.getPerspectiveTransform(corners, np.float32([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]]))


def process(f):
    r = cv2.warpPerspective(f, M, (W, H))
    return wb.clean(r), r


def with_person(x, base=room):
    f = base.copy()
    f[300:1080, x:x + 350] = (150, 170, 200)
    f[300:1080, x:x + 350] += rng.integers(0, 6, (780, 350, 3), dtype=np.uint8)  # sensor noise
    return f


def writer(k, base):
    """Person in light striped clothes writing: still torso, moving arm."""
    f = base.copy()
    f[350:1080, 420:760] = (195, 200, 205)  # still, light torso (close to the board's color)
    for y in range(350, 1080, 40):
        f[y:y + 12, 420:760] = (150, 155, 165)  # stripes / folds
    f[350:1080, 420:760] += rng.integers(0, 6, (730, 340, 3), dtype=np.uint8)
    ya = 420 + int(60 * np.sin(k * 0.9))  # moving arm
    f[ya:ya + 60, 760:960] = (150, 160, 175)
    return f


def diff(a, b):
    return np.abs(a.astype(int) - b).mean()


def feed(board, frames):
    for f in frames:
        c, r = process(f)
        board.update(c, r)


ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("OK    " if cond else "FAIL  ") + name)


clean0, _ = process(room)

# person standing in front of the ink
b = wb.StableBoard()
feed(b, [room] * 13)
feed(b, [with_person(300 + i * 30) for i in range(10)])
feed(b, [with_person(450)] * 24)
check("person still for 6 s in front of the ink: not baked in", diff(b.board, clean0) < 0.3)
feed(b, [room] * 14)
check("person leaves: board intact", diff(b.board, clean0) < 0.3)
feed(b, [with_person(450)] * 56)
check("still for 14 s: baked in (known limit)", diff(b.board, clean0) >= 0.3)

# writing, then erasing
b = wb.StableBoard()
feed(b, [room] * 13)
f2 = room.copy()
cv2.putText(f2, "y = 3x - 2", (800, 750), cv2.FONT_HERSHEY_SIMPLEX, 2, (30, 30, 30), 4)
feed(b, [with_person(1150 + i * 10, f2) for i in range(8)])
feed(b, [f2] * 16)
clean2, _ = process(f2)
check("writing shows up ~4 s after stepping away", diff(b.board, clean2) < 0.3)
feed(b, [room] * 16)
check("erasing shows up", diff(b.board, clean0) < 0.3)

# long writing session without moving the torso
b = wb.StableBoard()
feed(b, [room] * 13)
f3 = room.copy()
cv2.putText(f3, "||x-y||=...", (520, 640), cv2.FONT_HERSHEY_SIMPLEX, 2, (30, 30, 30), 4)
feed(b, [writer(k, f3) for k in range(100)])  # 25 s
torso = b.board[300:1000, 60:470].astype(int)  # torso area in board coordinates
check("writes for 25 s without moving the torso: not baked in", (torso < 200).mean() < 0.02)
feed(b, [f3] * 16)
clean3, _ = process(f3)
check("person leaves: formula visible, board clean", diff(b.board, clean3) < 0.3)

# camera monitoring
shifted = np.roll(room, (20, 30), axis=(0, 1))
m = wb.CameraMonitor()
m.reset(room)
check("no false alarm when someone moves", all(m.check(with_person(300 + i * 40)) == "ok" for i in range(20)))
check("moved phone detected", [m.check(shifted) for _ in range(5)][-1] == "moved")
check("warning clears when the shift is gone", [m.check(room) for _ in range(5)][-1] == "ok")
m = wb.CameraMonitor()
m.reset(room)
_ = [m.check(shifted) for _ in range(2)]
check("brief shift (0.5 s) triggers nothing", m.check(room) == "ok")
check("black frame detected", wb.CameraMonitor().check(np.zeros_like(room) + 10) == "black")

t0 = time.time()
for _ in range(10):
    c, r = process(room)
    b.update(c, r)
    m.check(room)
print(f"{(time.time() - t0) * 100:.0f} ms per sample")
print("ALL OK" if ok else "SOME TESTS FAILED")
sys.exit(0 if ok else 1)
