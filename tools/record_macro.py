"""
record_macro.py — records real mouse movement/clicks/scrolls to a JSON file
so Claude can see EXACTLY what a working interaction looks like (coordinates,
drag distances, timing) instead of guessing at synthetic input that this
particular UI doesn't reliably respond to.

Usage:
    python tools/record_macro.py

Recording starts immediately. Do the exact sequence you want captured
(e.g., scroll the island list up to Rookie Island, click it, click Stage 1,
click Start) at your normal pace — no need to rush. Press ESC when done.

Saves to recorded_macro.json in this folder.
"""

from __future__ import annotations

import json
import threading
import time

from pynput import mouse, keyboard

events: list[dict] = []
start_time = 0.0

_last_move_t = 0.0
_MOVE_SAMPLE_INTERVAL = 0.02  # seconds — throttles move events to ~50/sec, still enough to reconstruct drags precisely


def _t() -> float:
    return round(time.time() - start_time, 4)


def on_move(x, y):
    global _last_move_t
    now = time.time()
    if now - _last_move_t >= _MOVE_SAMPLE_INTERVAL:
        _last_move_t = now
        events.append({"type": "move", "t": _t(), "x": x, "y": y})


def on_click(x, y, button, pressed):
    events.append({"type": "click", "t": _t(), "x": x, "y": y, "button": str(button), "pressed": pressed})


def on_scroll(x, y, dx, dy):
    events.append({"type": "scroll", "t": _t(), "x": x, "y": y, "dx": dx, "dy": dy})


def on_press(key):
    if key == keyboard.Key.esc:
        return False  # stops the keyboard listener, which ends recording


def _save() -> None:
    out_path = "recorded_macro.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)
    print(f"Saved {len(events)} events to {out_path}")


def _autosave_loop(stop_flag: threading.Event) -> None:
    """Saves to disk every second regardless of how recording eventually
    ends — a forced process kill (e.g. taskkill) doesn't run Python's
    finally blocks, so this is the real safety net, not the try/finally
    below."""
    while not stop_flag.wait(1.0):
        _save()


def main() -> None:
    global start_time
    print("Recording NOW. Do the sequence (scroll to Rookie Island -> click it -> click Stage 1 -> click Start).")
    print("Press ESC when done.")
    start_time = time.time()

    stop_flag = threading.Event()
    autosave_thread = threading.Thread(target=_autosave_loop, args=(stop_flag,), daemon=True)
    autosave_thread.start()

    m_listener = mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)
    k_listener = keyboard.Listener(on_press=on_press)
    m_listener.start()
    k_listener.start()
    try:
        k_listener.join()
    finally:
        m_listener.stop()
        stop_flag.set()
        _save()


if __name__ == "__main__":
    main()
