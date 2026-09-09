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
import mimetypes
import os
import queue
import threading
import tkinter as tk

import cv2
import mss
import numpy as np
import webview

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x80000
_WS_EX_TRANSPARENT = 0x20
_LWA_COLORKEY = 0x1

_BANNER_RED = "#ff2b2b"
_ELO_LIGHT_BLUE = "#66ccff"
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
        pvp_running_event: threading.Event | None = None,
        pvp_log_queue: "queue.Queue[str] | None" = None,
        pvp_spam=None,
        auto_clicker_running_event: threading.Event | None = None,
        auto_clicker_log_queue: "queue.Queue[str] | None" = None,
        auto_clicker=None,
        hotkey_listener=None,
        config_path: str = "config.yaml",
    ):
        self.story_running_event = story_running_event
        self.towers_running_event = towers_running_event
        self.towers_automation = towers_automation
        self.log_queue = log_queue
        self.on_close = on_close
        self.pvp_running_event = pvp_running_event
        self.pvp_log_queue = pvp_log_queue
        self.pvp_spam = pvp_spam
        self.auto_clicker_running_event = auto_clicker_running_event
        self.auto_clicker_log_queue = auto_clicker_log_queue
        self.auto_clicker = auto_clicker
        self.hotkey_listener = hotkey_listener
        self.config_path = config_path

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
        # single-thread requirements — resolved by giving Tk its own
        # dedicated thread for its entire lifecycle (created there, pumped
        # there) and reserving the actual main thread for webview.start().
        self._overlay_ready = threading.Event()

        # -- visible control panel, pywebview --
        hk = config["hotkeys"]
        self._hotkeys = {
            "start": hk["start"].upper(),
            "stop": hk["stop"].upper(),
            "towers": hk.get("towers_toggle", "f8").upper(),
            "pvp": hk.get("pvp_toggle", "p").upper(),
            "autoclicker": hk.get("auto_clicker_toggle", "c").upper(),
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
        return {"bg_color": self.bg_color, "bg_image": bg_image, "hotkeys": self._hotkeys}

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

        return {
            "story_on": self.story_running_event.is_set(),
            "towers_on": self.towers_running_event.is_set(),
            "towers_stats": towers_stats,
            "towers_log": self._drain(self.log_queue),
            "pvp_on": self.pvp_running_event is not None and self.pvp_running_event.is_set(),
            "pvp_clicks": self.pvp_spam.stats["clicks"] if self.pvp_spam is not None else 0,
            "pvp_log": self._drain(self.pvp_log_queue),
            "autoclicker_on": self.auto_clicker_running_event is not None and self.auto_clicker_running_event.is_set(),
            "autoclicker_clicks": self.auto_clicker.stats["clicks"] if self.auto_clicker is not None else 0,
            "autoclicker_log": self._drain(self.auto_clicker_log_queue),
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

    # -- AFK banner (Toplevel, borderless, always-on-top) — unchanged from gui.py --

    def _build_banner(self) -> None:
        banner = tk.Toplevel(self._overlay_root)
        self._banner = banner
        banner.overrideredirect(True)
        banner.attributes("-topmost", True)
        banner.configure(bg="black")
        banner.attributes("-transparentcolor", "black")

        tk.Label(
            banner, text=self.banner_text, font=("Arial Black", 96, "bold"),
            fg=_BANNER_RED, bg="black",
        ).pack(padx=20, pady=20)

        self._reposition_banner()
        banner.withdraw()
        _make_click_through(banner)

        floor_overlay = tk.Toplevel(self._overlay_root)
        self._floor_overlay = floor_overlay
        floor_overlay.overrideredirect(True)
        floor_overlay.attributes("-topmost", True)
        floor_overlay.configure(bg="black")
        floor_overlay.attributes("-transparentcolor", "black")
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

        tk.Label(
            overlay, text=self.elo_overlay_text, font=("Arial Black", 40, "bold"),
            fg=_ELO_LIGHT_BLUE, bg=_ELO_PANEL_BLUE,
        ).pack(padx=20, pady=(8, 0))
        self._elo_label = tk.Label(overlay, bg=_ELO_PANEL_BLUE)
        self._elo_label.pack(padx=20, pady=(4, 10))

        self._reposition_elo_overlay()
        overlay.withdraw()
        _make_click_through(overlay)

    def _reposition_elo_overlay(self) -> None:
        overlay = self._elo_overlay
        overlay.update_idletasks()
        w = overlay.winfo_reqwidth()
        sw = overlay.winfo_screenwidth()
        overlay.geometry(f"+{(sw - w) // 2}+20")

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
        banner = self._banner
        banner.update_idletasks()
        w, h = banner.winfo_reqwidth(), banner.winfo_reqheight()
        sw, sh = banner.winfo_screenwidth(), banner.winfo_screenheight()
        banner.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

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
        tracer.attributes("-transparentcolor", "black")

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

        running = story_on or towers_on or autoclicker_on
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

            if towers_on and not self._tracer_visible:
                self._tracer.deiconify()
                self._tracer_visible = True
            elif not towers_on and self._tracer_visible:
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

        if towers_on:
            self._draw_tracers()

        self._overlay_root.after(200, self._poll_overlays)

    def hide_overlays_for_capture(self, timeout: float = 0.3) -> None:
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
