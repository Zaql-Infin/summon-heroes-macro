"""
capture_template.py — interactive helper to cut a template image out of a
live screenshot (e.g. the teleport button, a door archway, the XP bar).

Usage:
    python tools/capture_template.py <output_path.png>

1. Switch to Roblox, get the UI element you want visible on screen.
2. Alt-tab back to this script's terminal and press Enter when ready
   (there's a 3-second countdown so you can switch to the game window).
3. A window opens showing the full screenshot. Click-drag a box around the
   element you want (e.g. the purple teleport button, or a door archway).
4. Press Enter/Space to confirm the crop, or C to cancel and redo it.
5. The crop is saved to the path you gave, ready to reference from
   config.yaml.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import mss
import numpy as np


def grab_full_screen() -> np.ndarray:
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        raw = sct.grab(monitor)
        img = np.array(raw)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python tools/capture_template.py <output_path.png>")
        sys.exit(1)

    out_path = Path(sys.argv[1])
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("Switch to Roblox now. Capturing in 3 seconds...")
    time.sleep(3)

    frame = grab_full_screen()
    print("Drag a box around the element, then press ENTER or SPACE. Press C to cancel.")

    # cv2.selectROI opens its own window with drag-to-select built in.
    roi = cv2.selectROI("Select template region", frame, showCrosshair=True)
    cv2.destroyAllWindows()

    x, y, w, h = roi
    if w == 0 or h == 0:
        print("No region selected, nothing saved.")
        return

    crop = frame[y:y + h, x:x + w]
    cv2.imwrite(str(out_path), crop)
    print(f"Saved template to {out_path} ({w}x{h}, top-left at {x},{y}).")
    print("If this is a screen coordinate (e.g. for a fallback_coordinate in "
          f"config.yaml), the click center is roughly ({x + w // 2}, {y + h // 2}).")


if __name__ == "__main__":
    main()
