"""
web_gui.py — control panel window, rebuilt on pywebview (HTML/CSS/JS)
instead of Tkinter (2026-09-09).

Why: the user wanted a real background image behind the control panel's
UI, with the buttons/panels themselves genuinely see-through (glass
panel look) over it. Tkinter cannot do this — a Frame/Label always paints
an opaque rectangle, confirmed via direct testing; there's no
per-widget transparency. A real browser engine (Windows' built-in
WebView2, via pywebview — no bundled Chromium, just points at the OS's
existing runtime) does this trivially with ordinary CSS
(background-image + backdrop-filter blur on a semi-transparent panel).

The four in-game overlays (AFK banner, floor-number mirror, PvP ELO
overlay, Towers tracer) are UNCHANGED from gui.py — still plain Tkinter
Toplevels using the same colorkey-transparency / click-through technique,
just now owned by a hidden Tk root (never shown) instead of a visible
one, since none of that code has anything to do with the control panel
redesign and was already working correctly.

Threading: pywebview's own event loop (webview.start()) blocks the
thread it's called from and wants to be the main thread on Windows, so
it owns run(). The hidden overlay Tk root's mainloop runs in a daemon
thread instead — unusual but fine here since nothing outside this class
ever touches Tk widgets directly; every cross-thread interaction already
goes through root.after(...) scheduling (see
hide_overlays_for_capture/show_overlays_after_capture), same pattern the
old gui.py already relied on.
"""

from __future__ import annotations

import base64
import ctypes
import math
import mimetypes
import os
import queue
import threading
import tkinter as tk

import cv2
import mss
import numpy as np
import webview

from . import input_sim

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x80000
_WS_EX_TRANSPARENT = 0x20
_LWA_COLORKEY = 0x1

_BANNER_RED = "#ff2b2b"
# User-requested (2026-09-12): themed to match the control panel's purple
# "bubbly" look instead of a flat red, with a gentle color pulse between
# these two shades — see _animate_banner.
_BANNER_PURPLE = "#a259ff"
_BANNER_PURPLE_BRIGHT = "#e6d1ff"
_ELO_LIGHT_BLUE = "#66ccff"
_ELO_LIGHT_BLUE_BRIGHT = "#d0f0ff"
_ELO_PANEL_BLUE = "#1565c0"
_TRACER_CHOSEN = "#00ff88"
_TRACER_OTHER = "#888888"

# Computed fresh (not module-level) from the CURRENT working directory —
# not __file__, which under PyInstaller's onefile freezing points into
# the temp extraction dir (_MEIPASS), not the actual exe's own folder.
# main.py already anchors cwd to the app's real directory for exactly
# this reason. Must be absolute: pywebview's own local server (confirmed
# live — "Error: 404 Not Found ... panel.html") doesn't resolve a bare
# relative path against cwd the way plain file I/O elsewhere in this app
# does; only an absolute path (or file:// URI) works reliably here.
def _html_path() -> str:
    return os.path.abspath(os.path.join("web_gui", "panel.html"))

# JS KeyboardEvent.key -> pynput Key-name mismatches worth covering
# explicitly; everything else is just key.lower(), which lines up for
# letters, digits, F-keys, and names already used elsewhere in config.yaml.
_REBIND_IGNORED_KEYS = {"Shift", "Control", "Alt", "Meta", "CapsLock", "NumLock", "ScrollLock"}
_REBIND_KEY_OVERRIDES = {
    "arrowleft": "left",
    "arrowright": "right",
    "arrowup": "up",
    "arrowdown": "down",
    " ": "space",
    "escape": "esc",
}


def _normalize_rebind_key(js_key: str) -> str | None:
    if js_key in _REBIND_IGNORED_KEYS:
        return None
    name = js_key.lower()
    return _REBIND_KEY_OVERRIDES.get(name, name)


def _set_transparent_color(window: tk.Misc, color: str) -> None:
    """Colorkey transparency (the named color renders as see-through) —
    "-transparentcolor" is a Windows-only Tk extension; stock Tk/Aqua on
    macOS doesn't support it at all and raises TclError. Guarded so a Mac
    run just gets an opaque overlay window (a known, documented limitation
    — see README's macOS section) instead of crashing on startup."""
    try:
        window.attributes("-transparentcolor", color)
    except tk.TclError:
        pass


def _make_click_through(window: tk.Misc) -> None:
    """OS-level click-through, identical technique to the old gui.py —
    see that module's history for why the explicit
    SetLayeredWindowAttributes call is needed on top of Tk's own
    -transparentcolor handling."""
    try:
        window.update_idletasks()
        hwnd = window.winfo_id()
        styles = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, styles | _WS_EX_LAYERED | _WS_EX_TRANSPARENT)
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0x000000, 0, _LWA_COLORKEY)
    except Exception:
        pass


class WebControlPanel:
    def __init__(
        self,
        story_running_event: threading.Event,
        towers_running_event: threading.Event,
        config: dict,
        towers_automation,
        log_queue: "queue.Queue[str]",
        on_close,
        campaign_running_event: threading.Event | None = None,
        pvp_running_event: threading.Event | None = None,
        pvp_log_queue: "queue.Queue[str] | None" = None,
        pvp_spam=None,
        auto_clicker_running_event: threading.Event | None = None,
        auto_clicker_log_queue: "queue.Queue[str] | None" = None,
        auto_clicker=None,
        skip_running_event: threading.Event | None = None,
        skip_log_queue: "queue.Queue[str] | None" = None,
        skip_farm=None,
        hotkey_listener=None,
        config_path: str = "config.yaml",
        towers_enabled: bool = True,
    ):
        self.story_running_event = story_running_event
        self.towers_running_event = towers_running_event
        self.towers_automation = towers_automation
        self.log_queue = log_queue
        self.on_close = on_close
        self.campaign_running_event = campaign_running_event
        self.pvp_running_event = pvp_running_event
        self.pvp_log_queue = pvp_log_queue
        self.pvp_spam = pvp_spam
        self.auto_clicker_running_event = auto_clicker_running_event
        self.auto_clicker_log_queue = auto_clicker_log_queue
        self.auto_clicker = auto_clicker
        self.skip_running_event = skip_running_event
        self.skip_log_queue = skip_log_queue
        self.skip_farm = skip_farm
        self.hotkey_listener = hotkey_listener
        self.config_path = config_path
        # User-requested (2026-09-13): dev vs public build split — off in a
        # public release until Towers is reliable enough to hand to other
        # people. See config.yaml's features.towers_enabled comment.
        self.towers_enabled = towers_enabled

        self.screen_region = config["screen"]["game_region"]

        overlay_cfg = config.get("overlay", {})
        self.banner_text = overlay_cfg.get("text", "AFK FARMING")
        self.floor_region = overlay_cfg.get("floor_region")
        self.floor_scale = overlay_cfg.get("floor_scale", 2.5)

        pvp_cfg = config.get("pvp_spam", {})
        self.elo_overlay_text = pvp_cfg.get("overlay_text", "AFK ELO FARMING")
        self.elo_region = pvp_cfg.get("elo_region")
        self.elo_scale = pvp_cfg.get("elo_scale", 3.0)

        gui_cfg = config.get("gui", {})
        self.bg_color = gui_cfg.get("background_color", "#150a1f")
        self._custom_bg_path = self._find_custom_background_path()

        self._sct = mss.mss()
        self._banner_visible = False
        self._tracer_visible = False
        self._elo_overlay_visible = False
        self._capture_in_progress = False
        self._floor_photo = None
        self._elo_photo = None

        # -- hidden Tk root, overlays only --
        # NOT created here — see run()'s comment for why: Tk must be
        # created AND have its mainloop pumped on the exact same thread
        # throughout its life (confirmed live: splitting the two raised
        # "RuntimeError: main thread is not in main loop" the moment a
        # worker thread called hide_overlays_for_capture), while pywebview
        # separately refuses outright to run anywhere except the real
        # process main thread (confirmed live: "WebViewException: pywebview
        # must be run on a main thread."). Two hard, conflicting
        # single-thread requirements — resolved (on Windows) by giving Tk
        # its own dedicated thread for its entire lifecycle (created there,
        # pumped there) and reserving the actual main thread for
        # webview.start().
        #
        # macOS (2026-09-13): this same split crashes hard — confirmed live,
        # "Terminating app due to uncaught exception of type NSException".
        # Windows tolerates a Tk root being created on a non-main thread;
        # macOS's Aqua/Cocoa backend does not — creating an NSWindow off the
        # main thread is a hard Cocoa error, not a Python exception, so it
        # can't be caught and just kills the process. And unlike Windows,
        # there's no free thread to hand Tk here: pywebview's Cocoa backend
        # is exactly as inflexible about needing the real main thread as its
        # Windows backend is, so both frameworks want the one thread macOS
        # actually allows GUI work on. Rather than a real fix (would mean
        # rebuilding the overlays as extra pywebview windows instead of Tk
        # ones — untested, unverifiable without a Mac), overlays are simply
        # skipped on macOS: run() never starts the Tk thread there, and
        # every method below that would touch self._overlay_root or the
        # per-overlay Tk widgets checks self._overlay_root is not None
        # first. Core automation (Story/Towers/PvP/Auto Clicker, the
        # control panel itself) is unaffected — only the four in-game
        # overlay windows (AFK banner, floor mirror, ELO overlay, Towers
        # tracer) are unavailable on macOS.
        self._overlay_root: tk.Tk | None = None
        self._overlay_ready = threading.Event()

        # -- visible control panel, pywebview --
        hk = config["hotkeys"]
        self._hotkeys = {
            "start": hk["start"].upper(),
            "stop": hk["stop"].upper(),
            "towers": hk.get("towers_toggle", "f8").upper(),
            "campaign": hk.get("campaign_toggle", "b").upper(),
            "pvp": hk.get("pvp_toggle", "p").upper(),
            "autoclicker": hk.get("auto_clicker_toggle", "c").upper(),
            "skip": hk.get("skip_toggle", "k").upper(),
        }
        self._webview_window = webview.create_window(
            "Summon Heroes Macro",
            url=_html_path(),
            js_api=self,
            width=800,
            height=600,
            x=60,
            y=60,
            on_top=True,
            resizable=False,
        )

    # -- backdrop image discovery / helpers -----------------------------------------

    def _find_custom_background_path(self) -> str | None:
        bg_dir = "backgrounds"
        if not os.path.isdir(bg_dir):
            return None
        exts = (".png", ".jpg", ".jpeg", ".bmp")
        candidates = sorted(
            f for f in os.listdir(bg_dir)
            if f.lower().startswith("background") and f.lower().endswith(exts)
        )
        return os.path.join(bg_dir, candidates[0]) if candidates else None

    def _image_to_data_uri(self, path: str) -> str | None:
        try:
            mime = mimetypes.guess_type(path)[0] or "image/png"
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:{mime};base64,{b64}"
        except Exception:
            return None

    # -- JS-callable API --------------------------------------------------------------

    def get_init_state(self) -> dict:
        bg_image = self._image_to_data_uri(self._custom_bg_path) if self._custom_bg_path else None
        return {
            "bg_color": self.bg_color,
            "bg_image": bg_image,
            "hotkeys": self._hotkeys,
            "towers_enabled": self.towers_enabled,
        }

    def get_state(self) -> dict:
        towers_stats = {"runtime": 0, "doors_detected": 0, "movements": 0, "teleports": 0, "recoveries": 0}
        if self.towers_automation is not None:
            s = self.towers_automation.stats
            towers_stats = {
                "runtime": self.towers_automation.runtime_seconds(),
                "doors_detected": s["doors_detected"],
                "movements": s["movements"],
                "teleports": s["teleports"],
                "recoveries": s["recoveries"],
            }

        skip_stats = {"runtime": 0, "doors_entered": 0, "farm_teleports": 0, "recoveries": 0}
        if self.skip_farm is not None:
            s = self.skip_farm.stats
            skip_stats = {
                "runtime": self.skip_farm.runtime_seconds(),
                "doors_entered": s["doors_entered"],
                "farm_teleports": s["farm_teleports"],
                "recoveries": s["recoveries"],
            }

        return {
            "story_on": self.story_running_event.is_set(),
            "campaign_on": self.campaign_running_event is not None and self.campaign_running_event.is_set(),
            "towers_on": self.towers_running_event.is_set(),
            "towers_stats": towers_stats,
            "towers_log": self._drain(self.log_queue),
            "pvp_on": self.pvp_running_event is not None and self.pvp_running_event.is_set(),
            "pvp_clicks": self.pvp_spam.stats["clicks"] if self.pvp_spam is not None else 0,
            "pvp_log": self._drain(self.pvp_log_queue),
            "autoclicker_on": self.auto_clicker_running_event is not None and self.auto_clicker_running_event.is_set(),
            "autoclicker_clicks": self.auto_clicker.stats["clicks"] if self.auto_clicker is not None else 0,
            "autoclicker_log": self._drain(self.auto_clicker_log_queue),
            "skip_on": self.skip_running_event is not None and self.skip_running_event.is_set(),
            "skip_stats": skip_stats,
            "skip_log": self._drain(self.skip_log_queue),
        }

    def _drain(self, q: "queue.Queue[str] | None") -> list:
        if q is None:
            return []
        lines = []
        try:
            while True:
                lines.append(q.get_nowait())
        except queue.Empty:
            pass
        return lines

    def start_story(self) -> None:
        self.story_running_event.set()
        self.towers_running_event.clear()

    def stop_story(self) -> None:
        self.story_running_event.clear()

    def start_towers(self) -> None:
        if not self.towers_enabled:
            return
        self.towers_running_event.set()
        self.story_running_event.clear()

    def pause_towers(self) -> None:
        self.towers_running_event.clear()

    def estop_towers(self) -> None:
        self.towers_running_event.clear()
        if self.towers_automation is not None:
            self.towers_automation.navigator.stop_all_movement()
        if self.log_queue is not None:
            self.log_queue.put("[INFO] Emergency stop — all inputs released")

    def start_pvp(self) -> None:
        if self.pvp_running_event is None:
            return
        self.pvp_running_event.set()
        self.story_running_event.clear()
        self.towers_running_event.clear()

    def stop_pvp(self) -> None:
        if self.pvp_running_event is not None:
            self.pvp_running_event.clear()

    def start_autoclicker(self) -> None:
        if self.auto_clicker_running_event is None:
            return
        self.auto_clicker_running_event.set()
        self.story_running_event.clear()
        self.towers_running_event.clear()

    def stop_autoclicker(self) -> None:
        if self.auto_clicker_running_event is not None:
            self.auto_clicker_running_event.clear()

    def start_skip(self) -> None:
        if self.skip_running_event is None:
            return
        self.skip_running_event.set()
        self.story_running_event.clear()
        self.towers_running_event.clear()

    def stop_skip(self) -> None:
        if self.skip_running_event is not None:
            self.skip_running_event.clear()

    def rebind_autoclicker(self, js_key: str) -> str:
        new_key = _normalize_rebind_key(js_key)
        if not new_key:
            return self._hotkeys["autoclicker"]
        self._hotkeys["autoclicker"] = new_key.upper()
        if self.hotkey_listener is not None:
            self.hotkey_listener.auto_clicker_key = new_key.lower()
        self._save_config_value("hotkeys", "auto_clicker_toggle", new_key.lower())
        return new_key.upper()

    def set_background_color(self, hex_color: str) -> None:
        self.bg_color = hex_color
        self._save_config_value("gui", "background_color", hex_color)

    def pick_background_image(self) -> str | None:
        try:
            result = self._webview_window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Image Files (*.png;*.jpg;*.jpeg;*.bmp)",),
            )
        except Exception:
            return None
        if not result:
            return None
        src = result[0]
        ext = os.path.splitext(src)[1].lower() or ".png"
        os.makedirs("backgrounds", exist_ok=True)
        # Clear any previous background.* before copying the new one in,
        # so _find_custom_background_path never picks up a stale leftover
        # alongside the new file.
        for f in os.listdir("backgrounds"):
            if f.lower().startswith("background") and f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                try:
                    os.remove(os.path.join("backgrounds", f))
                except OSError:
                    pass
        dest = os.path.join("backgrounds", "background" + ext)
        img = cv2.imread(src)
        if img is None:
            return None
        cv2.imwrite(dest, img)
        self._custom_bg_path = dest
        return self._image_to_data_uri(dest)

    def _save_config_value(self, section: str, key: str, value) -> None:
        try:
            from ruamel.yaml import YAML
            yaml_rt = YAML()
            yaml_rt.preserve_quotes = True
            yaml_rt.indent(mapping=2, sequence=4, offset=2)
            with open(self.config_path, "r", encoding="utf-8") as f:
                doc = yaml_rt.load(f)
            doc.setdefault(section, {})[key] = value
            with open(self.config_path, "w", encoding="utf-8", newline="\n") as f:
                yaml_rt.dump(doc, f)
        except Exception:
            pass

    # -- AFK banner (Toplevel, borderless, always-on-top) --

    def _build_banner(self) -> None:
        # User-requested (2026-09-12): moved to the top of the screen,
        # shrunk way down, and given a gentle bubbly pulse/bob animation to
        # match the control panel's theme instead of one giant static red
        # wall of text — see _reposition_banner (top placement) and
        # _animate_banner (the pulse/bob loop).
        banner = tk.Toplevel(self._overlay_root)
        self._banner = banner
        banner.overrideredirect(True)
        banner.attributes("-topmost", True)
        banner.configure(bg="black")
        _set_transparent_color(banner, "black")

        self._banner_label = tk.Label(
            banner, text=self.banner_text, font=("Arial Black", 30, "bold"),
            fg=_BANNER_PURPLE, bg="black",
        )
        self._banner_label.pack(padx=16, pady=8)
        self._banner_anim_tick = 0
        self._banner_base_y = 16

        self._reposition_banner()
        banner.withdraw()
        _make_click_through(banner)
        self._animate_banner()

        floor_overlay = tk.Toplevel(self._overlay_root)
        self._floor_overlay = floor_overlay
        floor_overlay.overrideredirect(True)
        floor_overlay.attributes("-topmost", True)
        floor_overlay.configure(bg="black")
        _set_transparent_color(floor_overlay, "black")
        self._floor_label = tk.Label(floor_overlay, bg="black")
        self._floor_label.pack(padx=10, pady=10)
        floor_overlay.withdraw()
        _make_click_through(floor_overlay)

    def _build_elo_overlay(self) -> None:
        overlay = tk.Toplevel(self._overlay_root)
        self._elo_overlay = overlay
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.configure(bg=_ELO_PANEL_BLUE)

        # User-requested (2026-09-12): shrunk down and given the same
        # gentle pulse animation as the AFK banner instead of one big
        # static title — already sits at the top, per that same request.
        self._elo_title_label = tk.Label(
            overlay, text=self.elo_overlay_text, font=("Arial Black", 20, "bold"),
            fg=_ELO_LIGHT_BLUE, bg=_ELO_PANEL_BLUE,
        )
        self._elo_title_label.pack(padx=14, pady=(6, 0))
        self._elo_label = tk.Label(overlay, bg=_ELO_PANEL_BLUE)
        self._elo_label.pack(padx=14, pady=(3, 7))
        self._elo_anim_tick = 0

        self._reposition_elo_overlay()
        overlay.withdraw()
        _make_click_through(overlay)
        self._animate_elo_overlay()

    def _reposition_elo_overlay(self) -> None:
        overlay = self._elo_overlay
        overlay.update_idletasks()
        w = overlay.winfo_reqwidth()
        sw = overlay.winfo_screenwidth()
        overlay.geometry(f"+{(sw - w) // 2}+20")

    def _animate_elo_overlay(self) -> None:
        """Same gentle pulse idea as _animate_banner, for the ELO overlay's
        title text — user-requested (2026-09-12)."""
        if self._elo_overlay_visible:
            self._elo_anim_tick += 1
            pulse = (math.sin(self._elo_anim_tick * 0.1) + 1) / 2
            self._elo_title_label.configure(fg=self._lerp_color(_ELO_LIGHT_BLUE, _ELO_LIGHT_BLUE_BRIGHT, pulse))
        self._elo_overlay.after(80, self._animate_elo_overlay)

    def _grab_elo_image(self) -> bytes | None:
        if not self.elo_region:
            return None
        x, y, w, h = self.elo_region
        try:
            raw = self._sct.grab({"left": x, "top": y, "width": w, "height": h})
        except Exception:
            return None
        img = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
        new_w, new_h = max(1, int(w * self.elo_scale)), max(1, int(h * self.elo_scale))
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
        ok, buf = cv2.imencode(".png", img)
        return buf.tobytes() if ok else None

    def _reposition_banner(self) -> None:
        # User-requested (2026-09-12): top of the screen, not screen-center
        # (which used to sit right over the middle of the gameplay view).
        banner = self._banner
        banner.update_idletasks()
        w = banner.winfo_reqwidth()
        sw = banner.winfo_screenwidth()
        banner.geometry(f"+{(sw - w) // 2}+{self._banner_base_y}")

    @staticmethod
    def _lerp_color(c1: str, c2: str, t: float) -> str:
        """Linear-interpolates between two "#rrggbb" hex colors at t in
        [0, 1] — used for the banner's gentle pulse (see _animate_banner)."""
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        r = round(r1 + (r2 - r1) * t)
        g = round(g1 + (g2 - g1) * t)
        b = round(b1 + (b2 - b1) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _animate_banner(self) -> None:
        """Gentle bubbly bob + color-pulse loop for the AFK banner, running
        independently of _poll_overlays (which also does screen grabs, so
        keeping this on its own lighter/faster tick keeps the animation
        smooth). User-requested (2026-09-12) to match the control panel's
        bubbly-animated look instead of a static wall of text."""
        if self._banner_visible:
            self._banner_anim_tick += 1
            t = self._banner_anim_tick
            bob = round(3 * math.sin(t * 0.15))
            banner = self._banner
            w = banner.winfo_reqwidth()
            sw = banner.winfo_screenwidth()
            banner.geometry(f"+{(sw - w) // 2}+{self._banner_base_y + bob}")
            pulse = (math.sin(t * 0.1) + 1) / 2
            self._banner_label.configure(fg=self._lerp_color(_BANNER_PURPLE, _BANNER_PURPLE_BRIGHT, pulse))
        self._banner.after(80, self._animate_banner)

    def _reposition_floor_overlay(self) -> None:
        overlay = self._floor_overlay
        overlay.update_idletasks()
        w, h = overlay.winfo_reqwidth(), overlay.winfo_reqheight()
        sw, sh = overlay.winfo_screenwidth(), overlay.winfo_screenheight()
        margin = 24
        overlay.geometry(f"+{sw - w - margin}+{sh - h - margin}")

    def _grab_floor_image(self) -> bytes | None:
        if not self.floor_region:
            return None
        x, y, w, h = self.floor_region
        try:
            raw = self._sct.grab({"left": x, "top": y, "width": w, "height": h})
        except Exception:
            return None
        img = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
        new_w, new_h = max(1, int(w * self.floor_scale)), max(1, int(h * self.floor_scale))
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
        ok, buf = cv2.imencode(".png", img)
        return buf.tobytes() if ok else None

    def _build_tracer_overlay(self) -> None:
        tracer = tk.Toplevel(self._overlay_root)
        self._tracer = tracer
        tracer.overrideredirect(True)
        tracer.attributes("-topmost", True)
        tracer.configure(bg="black")
        _set_transparent_color(tracer, "black")

        rx, ry, rw, rh = self.screen_region
        tracer.geometry(f"{rw}x{rh}+{rx}+{ry}")

        self._tracer_canvas = tk.Canvas(tracer, width=rw, height=rh, bg="black", highlightthickness=0)
        self._tracer_canvas.pack()
        tracer.withdraw()
        _make_click_through(tracer)

    def _draw_tracers(self) -> None:
        canvas = self._tracer_canvas
        canvas.delete("all")
        if self.towers_automation is None:
            return

        navigator = self.towers_automation.navigator
        rx, ry, rw, rh = self.screen_region
        cx_screen = rw / 2

        chosen = navigator.last_chosen_match
        for name, m in navigator.last_all_candidates:
            is_chosen = (m is chosen)
            color = _TRACER_CHOSEN if is_chosen else _TRACER_OTHER
            x1, y1 = m.x - rx, m.y - ry
            x2, y2 = x1 + m.w, y1 + m.h
            canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=3 if is_chosen else 2)
            label = f"{name} {m.score:.2f}" + (" ← chosen" if is_chosen else "")
            canvas.create_text(x1, y1 - 10, text=label, fill=color, anchor="sw",
                                font=("Consolas", 11, "bold"))
            if is_chosen:
                cx, cy = x1 + m.w / 2, y1 + m.h / 2
                canvas.create_line(cx_screen, rh, cx, cy, fill=_TRACER_CHOSEN, width=2)
                canvas.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, outline=_TRACER_CHOSEN, width=2)

    def _poll_overlays(self) -> None:
        story_on = self.story_running_event.is_set()
        towers_on = self.towers_running_event.is_set()
        pvp_on = self.pvp_running_event is not None and self.pvp_running_event.is_set()
        autoclicker_on = self.auto_clicker_running_event is not None and self.auto_clicker_running_event.is_set()

        # User-requested (2026-09-12): "make it so it doesn't affect
        # anything else on the PC" — actual process/DLL injection into
        # Roblox was declined (would cross into cheat-injection territory,
        # a bannable ToS violation, and this project has been pure
        # external input-simulation from the start). What injection would
        # have actually bought here — the overlays only being visible over
        # the game, not lingering on top of other windows after alt-tab —
        # is achieved the safe way instead: hide them whenever Roblox isn't
        # the focused window, same window-title check hotkeys.py now uses.
        roblox_focused = input_sim.is_roblox_foreground()

        running = (story_on or towers_on or autoclicker_on) and roblox_focused
        pvp_on = pvp_on and roblox_focused
        towers_visible = towers_on and roblox_focused
        if not self._capture_in_progress:
            if running and not self._banner_visible:
                self._banner.deiconify()
                self._floor_overlay.deiconify()
                self._banner_visible = True
            elif not running and self._banner_visible:
                self._banner.withdraw()
                self._floor_overlay.withdraw()
                self._banner_visible = False

            if pvp_on and not self._elo_overlay_visible:
                self._elo_overlay.deiconify()
                self._elo_overlay_visible = True
            elif not pvp_on and self._elo_overlay_visible:
                self._elo_overlay.withdraw()
                self._elo_overlay_visible = False

            if towers_visible and not self._tracer_visible:
                self._tracer.deiconify()
                self._tracer_visible = True
            elif not towers_visible and self._tracer_visible:
                self._tracer.withdraw()
                self._tracer_visible = False

        if running and self.floor_region:
            png_bytes = self._grab_floor_image()
            if png_bytes:
                photo = tk.PhotoImage(data=png_bytes)
                self._floor_photo = photo
                self._floor_label.configure(image=photo)
                self._reposition_floor_overlay()

        if pvp_on and self.elo_region:
            png_bytes = self._grab_elo_image()
            if png_bytes:
                photo = tk.PhotoImage(data=png_bytes)
                self._elo_photo = photo
                self._elo_label.configure(image=photo)
                self._reposition_elo_overlay()

        if towers_visible:
            self._draw_tracers()

        self._overlay_root.after(200, self._poll_overlays)

    def hide_overlays_for_capture(self, timeout: float = 0.3) -> None:
        if self._overlay_root is None:
            return  # macOS — overlays never started, nothing to hide
        self._capture_in_progress = True
        done = threading.Event()

        def _do():
            if self._banner_visible:
                self._banner.withdraw()
                self._floor_overlay.withdraw()
            if self._elo_overlay_visible:
                self._elo_overlay.withdraw()
            if self._tracer_visible:
                self._tracer.withdraw()
            done.set()

        self._overlay_root.after(0, _do)
        done.wait(timeout)

    def show_overlays_after_capture(self) -> None:
        if self._overlay_root is None:
            return  # macOS — overlays never started, nothing to show
        def _do():
            if self._banner_visible:
                self._banner.deiconify()
                self._floor_overlay.deiconify()
            if self._elo_overlay_visible:
                self._elo_overlay.deiconify()
            if self._tracer_visible:
                self._tracer.deiconify()
            self._capture_in_progress = False

        self._overlay_root.after(0, _do)

    def _handle_webview_closed(self) -> None:
        self.on_close()
        if self._overlay_root is not None:
            self._overlay_root.after(0, self._overlay_root.quit)

    def _run_overlay_thread(self) -> None:
        """Tk's entire lifecycle — creation through mainloop — runs on this
        one dedicated thread, never the real process main thread (that's
        reserved for webview.start(), which refuses to run anywhere else).
        See the comment on self._overlay_ready in __init__ for why this
        split exists at all."""
        self._overlay_root = tk.Tk()
        self._overlay_root.withdraw()
        self._build_banner()
        self._build_elo_overlay()
        self._build_tracer_overlay()
        self._overlay_root.after(0, self._poll_overlays)
        self._overlay_ready.set()
        self._overlay_root.mainloop()

    def run(self) -> None:
        # macOS: skip the Tk overlay thread entirely — see the long comment
        # on self._overlay_root in __init__ for why (creating a Tk root off
        # the main thread is a hard, uncatchable Cocoa crash there, and
        # there's no spare main thread to give it instead since pywebview's
        # Cocoa backend needs that one too). self._overlay_root stays None,
        # which every method that would otherwise touch it already checks
        # for. Everything else (control panel, all automation modes) is
        # unaffected.
        if input_sim.IS_MACOS:
            self._overlay_ready.set()  # nothing to wait for — skip the 5s timeout below
        else:
            threading.Thread(target=self._run_overlay_thread, daemon=True, name="OverlayTkLoop").start()
        # towers.py/story_campaign.py's hide_overlays_fn/show_overlays_fn
        # get wired up to methods on this object immediately after
        # construction (see main.py) — harmless if the underlying Tk
        # objects don't exist yet at THAT point (nothing calls them until
        # a mode actually starts), but wait here anyway so the overlay
        # windows are guaranteed ready before the control panel (and thus
        # the user) can possibly trigger anything that needs them.
        self._overlay_ready.wait(timeout=5.0)
        self._webview_window.events.closed += self._handle_webview_closed
        webview.start()
