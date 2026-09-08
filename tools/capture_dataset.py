"""
capture_dataset.py — grabs raw screenshots at an interval while you play, to
build up a labeling dataset for the door-detector model (see label_doors.py
and train_door_detector.py). Run this alongside a normal play session (with
Towers off, so nothing is fighting you for input) and just play through as
many floors/door types as you can — variety across floors, lighting, and
door types is what actually makes the trained model robust.

Usage:
    python tools/capture_dataset.py [--interval 4] [--out dataset/raw]

Press Ctrl+C in this terminal to stop.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import cv2
import mss
import numpy as np


def grab_full_screen() -> np.ndarray:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        return cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=4.0, help="Seconds between captures.")
    parser.add_argument("--out", default="dataset/raw", help="Output folder for raw screenshots.")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    existing = list(out_dir.glob("frame_*.png"))
    count = len(existing)
    print(f"Capturing every {args.interval}s into {out_dir} (already have {count}). Ctrl+C to stop.")

    try:
        while True:
            frame = grab_full_screen()
            path = out_dir / f"frame_{count:04d}.png"
            cv2.imwrite(str(path), frame)
            count += 1
            print(f"Saved {path.name} (total {count})")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nStopped. {count} raw screenshots in {out_dir}.")


if __name__ == "__main__":
    main()
