# Whiteboard scanner

Turn a phone into a whiteboard scanner. The phone, placed in front of the
board, streams its camera over Wi-Fi; this script picks up the stream on the
PC, **rectifies** the board, **cleans** the image (uniform white background,
high-contrast ink, marker colors preserved), **erases the person standing in
front of it** and **saves** the board as PNG files, with a PDF of the whole
session at the end.

A DIY alternative to commercial whiteboard cameras (Kaptivo, Logitech
Scribe…): no hardware to buy, just a phone and a PC.

## Installation

```bash
python -m venv ~/.venvs/whiteboard
~/.venvs/whiteboard/bin/pip install -r requirements.txt
```

On the phone: **IP Webcam** (Android, stream at `http://PHONE_IP:8080/video`)
or **DroidCam** (`http://PHONE_IP:4747/video`). The phone and the PC must be on
the same Wi-Fi network.

## Usage

```bash
./whiteboard.sh http://PHONE_IP:8080/video --size 120x90
```

`whiteboard.sh` runs the script in the right environment (venv, window through
XWayland on Wayland) and copies all output to `logs/`. Use another venv with
`WHITEBOARD_VENV=/path/to/venv`. The launcher is a Bash script tested on Linux
(Wayland); elsewhere, run `python whiteboard.py …` directly.

On first launch, click the **4 corners of the white area** of the board, then
press **Enter**. The calibration is kept for the next sessions.

| Key      | Action |
|----------|--------|
| `s`      | capture (always kept) |
| `v`      | view: stabilized board / live cleaned / raw camera |
| `c`      | recalibrate (click the 4 corners again; `q` cancels and keeps the old ones) |
| `r`      | reset the stabilized board |
| `q`, Esc | quit (writes the session PDF) |

| Option | Default | Purpose |
|--------|---------|---------|
| `--size WxH` | `120x90` | board dimensions in cm (for the aspect ratio) |
| `--width` | `1600` | width of the rectified image in pixels |
| `--recalibrate` | | ignore the saved calibration |
| `--delay` | `3` | seconds of calm before an automatic capture |
| `--max-auto` | `3` | automatic captures kept (most recent ones) |
| `--fps` | `10` | displayed frames per second (lower = cooler PC) |
| `--freeze-if-moved` | | freeze the board when the phone seems to have moved |
| `--dir` | `captures` | captures folder |

Captures go to `captures/YYYY-MM-DD_HHhMM/`:
`NNN_HHMMSS_manual.png`, `NNN_HHMMSS_auto.png` and `session.pdf`.

## How it works

1. **Stream reading** in a background thread: the stream is drained
   continuously (no accumulating lag) and only ~12 frames/s are decoded to
   color.
2. **Rectification** with a homography computed from the 4 clicked corners.
3. **Cleaning**: estimate the background lighting (downscale, dilate, blur),
   divide the image by it, apply a gamma curve; near-white becomes pure white.
4. **Stabilized board**, 4 times per second, on a 32×20 grid:
   - motion is measured on the **raw** image (cleaning turns a person almost
     white, hence invisible);
   - a cell is only updated after 3 s without motion;
   - a large changed blob (person, object) must wait 10 more seconds, and if
     **any part** of a blob moves (the writing arm), the **whole** blob is
     blocked — a still back or torso does not get baked into the board.
5. **Camera monitoring**: black frame (camera covered) and moved phone (phase
   correlation on the full frame, warning only).
6. **Automatic capture** when the board has changed and then stays calm; an
   empty board is ignored.

## Known limitations

- Standing **completely still** (arm included) in front of the board for more
  than ~13 s: the person ends up in the board. Stepping aside or pressing `r`
  fixes it.
- Lamp or window reflections show up as light patches; adjust the phone or
  lighting placement.
- If the phone clearly moves, recalibrate with `c`.

## Tests

```bash
python tests/test_whiteboard.py
```

Tests on simulated frames (~1 min): person standing in front of the ink, long
writing session with a still torso, erasing, moved phone, black frame… Must
end with `ALL OK`.
