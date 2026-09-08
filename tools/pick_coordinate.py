"""
pick_coordinate.py — reports live mouse position so you can fill in
screen-space coordinates (e.g. teleport_button.fallback_coordinate, or
watchdog.stuck.sample_region) without guessing.

Usage:
    python tools/pick_coordinate.py

Move your mouse over the point/region you care about in the game window;
the current coordinate prints continuously. Ctrl+C to stop.
"""

from __future__ import annotations

import time

import pyautogui


def main() -> None:
    print("Move your mouse over the game window. Press Ctrl+C to stop.\n")
    try:
        while True:
            x, y = pyautogui.position()
            print(f"\rmouse: ({x:5d}, {y:5d})", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
