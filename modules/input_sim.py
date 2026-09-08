"""
input_sim.py — OS-level input simulation only.

Everything here goes through pynput/pyautogui, i.e. the same input path a
real keyboard/mouse would use. There is no process injection, no memory
access, and no reading/writing of game files anywhere in this module.
"""

from __future__ import annotations

import ctypes
import time

import pyautogui
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Controller as MouseController, Button

pyautogui.FAILSAFE = True  # slamming the mouse into a screen corner aborts pyautogui calls

_MOUSEEVENTF_MOVE = 0x0001


def _relative_mouse_move(dx: int, dy: int) -> None:
    """A genuinely relative mouse-motion event via the raw Win32 API, not
    pynput's Controller.move() — that call is still built on SetCursorPos
    (an absolute cursor teleport) under the hood on Windows. Games that
    capture mouse-look via raw/locked input (Roblox included, while a
    camera-drag button is held) read relative motion deltas and silently
    ignore SetCursorPos, which is why the drag previously did nothing."""
    ctypes.windll.user32.mouse_event(_MOUSEEVENTF_MOVE, int(dx), int(dy), 0, 0)

_keyboard = KeyboardController()
_mouse = MouseController()

# pynput needs special Key.* objects for non-character keys.
_SPECIAL_KEYS = {
    "space": Key.space,
    "shift": Key.shift,
    "ctrl": Key.ctrl,
    "enter": Key.enter,
    "esc": Key.esc,
    "tab": Key.tab,
    "left": Key.left,
    "right": Key.right,
    "up": Key.up,
    "down": Key.down,
}


def _resolve_key(key_name: str):
    return _SPECIAL_KEYS.get(key_name.lower(), key_name)


def key_down(key_name: str) -> None:
    _keyboard.press(_resolve_key(key_name))


def key_up(key_name: str) -> None:
    _keyboard.release(_resolve_key(key_name))


def tap_key(key_name: str, hold_seconds: float = 0.05) -> None:
    key_down(key_name)
    try:
        time.sleep(hold_seconds)
    finally:
        key_up(key_name)


def hold_key_for(key_name: str, seconds: float) -> None:
    key_down(key_name)
    try:
        time.sleep(seconds)
    finally:
        key_up(key_name)


def release_all(*key_names: str) -> None:
    """Safety helper — call on stop/panic so no movement key is left stuck down."""
    for k in key_names:
        try:
            key_up(k)
        except Exception:
            pass


def move_to(x: int, y: int, steps: int = 12, step_delay: float = 0.008) -> None:
    """Moves the cursor to (x, y) using genuine relative motion events (see
    _relative_mouse_move), not an absolute position teleport. Confirmed
    live: some UI buttons (Story menu) don't register a click that
    teleports straight there with no real movement in between — same root
    issue as the camera-drag and list-scroll-drag fixes earlier, just
    affecting plain clicks on this particular UI too.

    Re-measures actual cursor position every step (via pynput's own
    .position, which reads GetCursorPos) rather than trusting the nominal
    delta sent — Windows pointer-acceleration can scale a given relative
    delta non-linearly, so a blind fixed-delta move can overshoot or
    undershoot the target; closing the loop like this converges on it
    regardless. Finishes with a short exact-correction pass so the cursor
    still lands precisely on (x, y) despite the approach being relative."""
    for _ in range(steps):
        cx, cy = _mouse.position
        remaining_x, remaining_y = x - cx, y - cy
        if abs(remaining_x) <= 1 and abs(remaining_y) <= 1:
            break
        _relative_mouse_move(remaining_x * 0.5, remaining_y * 0.5)
        time.sleep(step_delay)
    for _ in range(5):
        cx, cy = _mouse.position
        remaining_x, remaining_y = x - cx, y - cy
        if remaining_x == 0 and remaining_y == 0:
            break
        _relative_mouse_move(remaining_x, remaining_y)
        time.sleep(step_delay)


def click_at(x: int, y: int, button: str = "left") -> None:
    """Screen-space click (not window-relative). Moves the cursor there
    with genuine relative motion first (see move_to) rather than an
    instant absolute teleport — some UI buttons don't register a click
    that arrives with no real movement beforehand."""
    btn = Button.left if button == "left" else Button.right
    move_to(x, y)
    time.sleep(0.02)  # tiny settle delay so the click registers where we expect
    _mouse.click(btn, 1)


def scroll_at(x: int, y: int, clicks: int) -> None:
    """Scrolls the mouse wheel at a screen position — for UI lists like the
    Story menu's island panel. Positive clicks scrolls up (toward earlier
    entries), negative scrolls down. This is a normal UI scroll (pynput's
    Controller.scroll, standard mouse-wheel event), not a camera drag.
    Moves there with genuine relative motion first (see move_to), same
    reasoning as click_at."""
    move_to(x, y)
    time.sleep(0.02)
    _mouse.scroll(0, clicks)


def drag_scroll(x: int, y: int, dy: int, steps: int = 15, hold_seconds: float = 0.4) -> None:
    """Click-drags vertically at a screen position — for Roblox UI lists
    that don't respond to the mouse wheel at all (confirmed live: the
    Story menu's island list ignored scroll_at entirely in both
    directions). dy > 0 drags downward (revealing content further UP the
    list, i.e. scrolling back toward the top); dy < 0 drags upward.

    Uses _relative_mouse_move (raw relative mouse_event), not absolute
    .position updates — same fix as drag_camera and for the same reason:
    confirmed live that setting .position repeatedly during a held-button
    drag produced literally zero effect here too, exactly like the
    camera-turn issue earlier — Roblox reads real relative motion deltas
    during a drag/held-button gesture, not absolute cursor teleports."""
    _mouse.position = (x, y)
    time.sleep(0.05)
    _mouse.press(Button.left)
    try:
        step_dy = dy / steps
        step_delay = hold_seconds / steps
        for _ in range(steps):
            _relative_mouse_move(0, step_dy)
            time.sleep(step_delay)
    finally:
        _mouse.release(Button.left)


def drag_camera(dx: int, seconds: float, button: str = "right", steps: int = 20) -> None:
    """Holds the given mouse button and turns the camera by dx pixels worth
    of movement over `seconds`, in small incremental steps rather than one
    big jump. While a mouse button is held for camera-look, Roblox (like
    most games) reads raw relative mouse-movement events, not the cursor's
    absolute screen position — so this uses a raw mouse_event relative
    move (see _relative_mouse_move) rather than pynput's Controller.move()
    /.position, both of which are absolute SetCursorPos teleports under the
    hood on Windows and get silently ignored for camera purposes.
    Always releases the button even if interrupted, so a scan tick can
    never leave it stuck down."""
    btn = Button.left if button == "left" else Button.right
    _mouse.press(btn)
    try:
        step_dx = dx / steps
        step_delay = seconds / steps
        for _ in range(steps):
            _relative_mouse_move(step_dx, 0)
            time.sleep(step_delay)
    finally:
        _mouse.release(btn)
