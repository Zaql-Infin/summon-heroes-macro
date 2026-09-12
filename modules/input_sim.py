"""
input_sim.py — OS-level input simulation only.

Everything here goes through pynput/pyautogui, i.e. the same input path a
real keyboard/mouse would use. There is no process injection, no memory
access, and no reading/writing of game files anywhere in this module.

Cross-platform (2026-09-12): Windows was the only target for most of this
project's life, so the raw-relative-mouse-motion trick below (needed because
Roblox's camera-look ignores absolute cursor teleports while a drag button
is held) was Win32-only (ctypes.windll.user32.mouse_event). macOS support
adds a Quartz Event Services equivalent — see _relative_mouse_move and
is_roblox_foreground. IMPORTANT: the macOS path has NOT been live-tested
against a real Roblox client (no Mac available to the person who wrote
this) — it's a best-effort port of the same fix Windows needed, not a
confirmed-working one. If camera-turning/dragging doesn't register on
macOS, that's the first place to look. See README's macOS section.
"""

from __future__ import annotations

import sys
import time

import pyautogui
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Controller as MouseController, Button

pyautogui.FAILSAFE = True  # slamming the mouse into a screen corner aborts pyautogui calls

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

if IS_WINDOWS:
    import ctypes
    _MOUSEEVENTF_MOVE = 0x0001
elif IS_MACOS:
    # pyobjc-framework-Quartz — only actually imported/required on macOS,
    # see requirements.txt. Optional at import time so this module still
    # loads (with degraded relative-move behavior, see below) if it's
    # somehow missing rather than crashing the whole app on startup.
    try:
        import Quartz
    except ImportError:
        Quartz = None
    try:
        from AppKit import NSWorkspace
    except ImportError:
        NSWorkspace = None


def _relative_mouse_move(dx: int, dy: int) -> None:
    """A genuinely relative mouse-motion event, not an absolute cursor
    teleport (pynput's Controller.move()/.position are SetCursorPos-style
    teleports under the hood on both platforms). Games that capture
    mouse-look via raw/locked input (Roblox included, while a camera-drag
    button is held) read relative motion deltas and silently ignore
    absolute repositioning, which is why a plain .move() previously did
    nothing for camera-turning.

    Windows: raw Win32 mouse_event — confirmed live, extensively, this
    session. macOS: Quartz CGEvent with the delta fields set directly —
    NOT live-verified (best-effort port, see module docstring)."""
    if IS_WINDOWS:
        ctypes.windll.user32.mouse_event(_MOUSEEVENTF_MOVE, int(dx), int(dy), 0, 0)
    elif IS_MACOS and Quartz is not None:
        event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (0, 0), Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaX, int(dx))
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaY, int(dy))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    else:
        # Last-resort fallback (Quartz unavailable, or an unexpected OS) —
        # an absolute move, which is known NOT to work for Roblox's
        # camera-look on Windows. Kept so the app still runs rather than
        # crashing, but camera-turning will likely be broken here.
        cx, cy = _mouse.position
        _mouse.position = (cx + dx, cy + dy)

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


def is_roblox_foreground(title_keyword: str = "roblox") -> bool:
    """True if the currently-foreground (focused) window/app's title
    contains `title_keyword` (case-insensitive) — used to gate hotkeys so
    they only fire while Roblox itself is focused, not whatever window
    happens to have focus. No process injection, no reading another
    process's memory — just asking the OS which window/app is frontmost.

    Windows: standard Win32 user32 calls (GetForegroundWindow/
    GetWindowTextW). macOS: NSWorkspace's frontmost-application name (the
    macOS Roblox client's app name is "RobloxPlayer", which still contains
    "roblox" case-insensitively, matching the same default keyword) — NOT
    live-verified (best-effort port, see module docstring)."""
    if IS_WINDOWS:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return False
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return title_keyword.lower() in buf.value.lower()
    elif IS_MACOS and NSWorkspace is not None:
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            name = app.localizedName() if app is not None else None
            return bool(name) and title_keyword.lower() in name.lower()
        except Exception:
            return False
    # Unknown platform, or macOS without pyobjc installed — don't silently
    # block every hotkey forever; let them through instead.
    return True


def foreground_app_name() -> str | None:
    """Diagnostic-only: the raw name is_roblox_foreground is actually
    comparing against, so a hotkey silently doing nothing can be logged
    with what the OS actually saw instead of just "nothing happened" —
    see hotkeys.py's blocked-hotkey log line."""
    if IS_WINDOWS:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return None
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    elif IS_MACOS and NSWorkspace is not None:
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return app.localizedName() if app is not None else None
        except Exception:
            return None
    return None
