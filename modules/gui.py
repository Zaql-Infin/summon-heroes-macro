"""
gui.py — control panel window (Story / Towers tabs) + the "AFK FARMING"
on-screen banner + the Towers tracer overlay.

Everything lives under one Tk root/mainloop (two Tk() instances across
threads is a fragile pattern — a root + Toplevels is the supported way to
have multiple windows). The banner is a borderless, always-on-top Toplevel
shown/hidden purely by polling whether either mode's running_event is set.

Our own windows must never appear in our own screenshot-based detection —
otherwise a) the tracer overlay's own drawn lines/boxes show up in the
*next* screenshot and get matched as false-positive "doors", and b) the
focus-guarantee click (navigation._ensure_game_focus) could land on our own
overlay instead of Roblox. SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)
would be the clean fix but fails outright on this system (confirmed via
GetLastError — not specific to our windows, WDA_NONE fails too), so instead
hide_overlays_for_capture()/show_overlays_after_capture() below hide every
overlay window for the brief instant of the actual screen grab and restore
them immediately after — tight enough to be imperceptible, called from
TowersAutomation's capture choke point (towers.py _check_for_door).

The Towers tab's activity log is fed from a background thread (the
TowersAutomation loop) through a thread-safe queue.Queue — Tk widgets are
only ever touched from this GUI thread's own poll loop (or via root.after
scheduling from other threads), never directly from the worker thread.

The tracer/banner/floor overlays are also marked WS_EX_TRANSPARENT (see
_make_click_through) so they never intercept mouse input regardless of what's
drawn on them — `-transparentcolor` only makes literal black pixels
click-through, so the tracer's own reference lines (drawn in the exact
center of the screen) were absorbing clicks meant for Roblox underneath,
including navigation.py's own focus-guarantee click.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import tkinter as tk

import cv2
import mss
import numpy as np

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x80000
_WS_EX_TRANSPARENT = 0x20


_LWA_COLORKEY = 0x1


def _make_click_through(window: tk.Misc) -> None:
    """OS-level click-through — verified working on this system (unlike
    SetWindowDisplayAffinity, which fails outright here). Every mouse event
    passes straight through to whatever's beneath, no matter what's drawn.

    Also explicitly (re)asserts the black colorkey via
    SetLayeredWindowAttributes, redundant with Tk's own -transparentcolor
    handling most of the time, but a third simultaneous layered/colorkeyed
    Toplevel (the PvP ELO overlay, added 2026-09-08) rendered as fully
    invisible — reported visible with correct geometry by
    IsWindowVisible/GetWindowRect, but nothing actually composited to the
    screen — even though its setup was identical to the working
    banner/floor overlays. Calling this directly removes any dependency on
    Tk's internal timing for that call."""
    try:
        window.update_idletasks()
        hwnd = window.winfo_id()
        styles = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, styles | _WS_EX_LAYERED | _WS_EX_TRANSPARENT)
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0x000000, 0, _LWA_COLORKEY)
    except Exception:
        pass

# Black/purple "bubbly" theme (2026-09-09) — the default background is
# live-customizable per user (see ControlPanel's color-mixer button in the
# header, and gui.background_color in config.yaml); everything else below
# is the fixed accent palette drawn on top of it.
_BG = "#150a1f"
_PANEL = "#2a1640"
_PANEL_LIGHT = "#3a2158"
_FG = "#f2e9ff"
_MUTED = "#b199d1"
_ACCENT_RUNNING = "#3ddc72"
_ACCENT_STOPPED = "#ff5c8a"
_ACCENT_PAUSED = "#ffb347"
_ACCENT_PURPLE = "#a259ff"
_ACCENT_PURPLE_BRIGHT = "#c58bff"
_BANNER_RED = "#ff2b2b"
_ELO_LIGHT_BLUE = "#66ccff"
_ELO_PANEL_BLUE = "#1565c0"
_TAB_ACTIVE = "#3a2158"
_TAB_INACTIVE = "#1c0e2b"
_TRACER_CHOSEN = "#00ff88"
_TRACER_OTHER = "#888888"
_TRACER_DEADZONE = "#ffcc00"
_TITLE_FONT = "Segoe Print"  # rounder/bubblier than Segoe UI; falls back gracefully if unavailable

# Modifier-only keysyms — ignored during hotkey rebinding since they can't
# be pressed on their own as a real toggle key (a bare Shift/Ctrl/Alt tap
# doesn't mean anything as a hotkey the way it does as a modifier).
_REBIND_IGNORED_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Caps_Lock", "Num_Lock", "Scroll_Lock",
}
# A few Tk keysym -> pynput Key-name mismatches worth covering explicitly
# (Tk's keysym and pynput's own Key enum names disagree here) — everything
# else is just keysym.lower(), which lines up for letters, digits, F-keys,
# and the arrow/space/etc. names already used elsewhere in config.yaml.
_REBIND_KEYSYM_OVERRIDES = {
    "escape": "esc",
    "return": "enter",
    "prior": "page_up",
    "next": "page_down",
}


def _tk_keysym_to_hotkey_name(keysym: str) -> str | None:
    if keysym in _REBIND_IGNORED_KEYSYMS:
        return None
    name = keysym.lower()
    return _REBIND_KEYSYM_OVERRIDES.get(name, name)


def _draw_mascot(canvas: tk.Canvas) -> None:
    """Small chibi header mascot — an original teal-twin-tails character
    silhouette (2026-09-09, per explicit user request for a "vocaloid-ish"
    feel in the header). Deliberately generic/original artwork, not any
    specific copyrighted character's actual design — just simple Canvas
    shapes evoking the same general vibe (teal twintails, chibi
    proportions) without reproducing anyone's protected character art."""
    teal = "#2fe0c9"
    teal_dark = "#1aa392"
    skin = "#ffe0c2"
    outfit = "#241238"
    outfit_trim = "#a259ff"

    # Twin tails, drawn first so the head/bangs layer over their roots.
    canvas.create_oval(2, 16, 20, 68, fill=teal, outline=teal_dark, width=1)
    canvas.create_oval(40, 16, 58, 68, fill=teal, outline=teal_dark, width=1)

    # Body (simple chibi torso).
    canvas.create_rectangle(18, 50, 42, 74, fill=outfit, outline="", width=0)
    canvas.create_oval(16, 46, 44, 58, fill=outfit, outline="")
    canvas.create_line(20, 56, 40, 56, fill=outfit_trim, width=2)

    # Head + bangs.
    canvas.create_oval(12, 6, 48, 42, fill=skin, outline="")
    canvas.create_arc(10, 2, 50, 34, start=0, extent=180, fill=teal, outline="", style="chord")
    canvas.create_oval(9, 16, 19, 26, fill=teal, outline="")
    canvas.create_oval(41, 16, 51, 26, fill=teal, outline="")

    # Face.
    canvas.create_oval(20, 22, 24, 27, fill="#241238", outline="")
    canvas.create_oval(36, 22, 40, 27, fill="#241238", outline="")
    canvas.create_oval(18, 29, 23, 33, fill="#ff9fc2", outline="")
    canvas.create_oval(37, 29, 42, 33, fill="#ff9fc2", outline="")


class ControlPanel:
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
        self._rebinding = False

        self.screen_region = config["screen"]["game_region"]  # [x, y, w, h]

        overlay_cfg = config.get("overlay", {})
        self.banner_text = overlay_cfg.get("text", "AFK FARMING")
        self.floor_region = overlay_cfg.get("floor_region")
        self.floor_scale = overlay_cfg.get("floor_scale", 2.5)
        self.interval = config["teleport_button"].get("interval_seconds", 2.5)

        pvp_cfg = config.get("pvp_spam", {})
        self.elo_overlay_text = pvp_cfg.get("overlay_text", "AFK ELO FARMING")
        self.elo_region = pvp_cfg.get("elo_region")
        self.elo_scale = pvp_cfg.get("elo_scale", 3.0)
        hk = config["hotkeys"]
        self.start_key = hk["start"].upper()
        self.stop_key = hk["stop"].upper()
        self.towers_key = hk.get("towers_toggle", "f8").upper()
        self.pvp_key = hk.get("pvp_toggle", "p").upper()
        self.auto_clicker_key = hk.get("auto_clicker_toggle", "c").upper()

        gui_cfg = config.get("gui", {})
        self.bg_color = gui_cfg.get("background_color", _BG)

        self._sct = mss.mss()
        self._banner_visible = False
        self._tracer_visible = False
        self._elo_overlay_visible = False
        self._capture_in_progress = False
        self._floor_photo = None  # keep a reference alive so Tk doesn't GC it
        self._elo_photo = None  # keep a reference alive so Tk doesn't GC it

        self.root = tk.Tk()
        self.root.title("Summon Heroes Macro")
        self.root.configure(bg=self.bg_color)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        # Pin a fixed on-screen position — without this, Tk's default window
        # placement can land on a secondary monitor (including one at
        # negative coordinates, off the primary screen entirely) depending
        # on session state, making the window seem to vanish.
        self.root.geometry("+60+60")
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close)

        self._build_header()
        self._build_tabs()
        self._build_story_tab()
        self._build_towers_tab()
        self._build_pvp_tab()
        self._build_auto_clicker_tab()
        self._show_tab("story")
        self._build_banner()
        self._build_elo_overlay()
        self._build_tracer_overlay()

        self.root.after(0, self._poll)

    # -- shared header + tab bar --------------------------------------------------

    def _build_header(self) -> None:
        row = tk.Frame(self.root, bg=self.bg_color)
        row.pack(fill="x", padx=18, pady=(18, 0))

        self._mascot_canvas = tk.Canvas(
            row, width=60, height=76, bg=self.bg_color, highlightthickness=0
        )
        self._mascot_canvas.pack(side="left", padx=(0, 10))
        _draw_mascot(self._mascot_canvas)

        title_col = tk.Frame(row, bg=self.bg_color)
        title_col.pack(side="left", fill="both", expand=True)
        tk.Label(
            title_col, text="Summon Heroes", font=(_TITLE_FONT, 20, "bold"), fg=_ACCENT_PURPLE_BRIGHT,
            bg=self.bg_color,
        ).pack(anchor="w")
        tk.Label(
            title_col, text="AFK Automation", font=("Segoe UI", 11), fg=_MUTED, bg=self.bg_color
        ).pack(anchor="w")

        self._bg_swatch_btn = tk.Canvas(
            row, width=26, height=26, bg=self.bg_color, highlightthickness=1,
            highlightbackground=_ACCENT_PURPLE, cursor="hand2",
        )
        self._bg_swatch_btn.pack(side="right", padx=(6, 0))
        self._bg_swatch_id = self._bg_swatch_btn.create_rectangle(
            2, 2, 24, 24, fill=self.bg_color, outline=""
        )
        self._bg_swatch_btn.bind("<Button-1>", lambda e: self._pick_background_color())
        palette_label = tk.Label(row, text="🎨", font=("Segoe UI", 13), bg=self.bg_color, cursor="hand2")
        palette_label.pack(side="right")
        palette_label.bind("<Button-1>", lambda e: self._pick_background_color())

    def _pick_background_color(self) -> None:
        from tkinter import colorchooser
        rgb, hex_color = colorchooser.askcolor(color=self.bg_color, title="Pick a background color", parent=self.root)
        if not hex_color:
            return
        self._apply_background_color(hex_color)
        self._save_config_value("gui", "background_color", hex_color)

    def _apply_background_color(self, new_color: str) -> None:
        old_color = self.bg_color
        self.bg_color = new_color
        self._recolor_tree(self.root, old_color, new_color)
        self._bg_swatch_btn.itemconfig(self._bg_swatch_id, fill=new_color)

    def _recolor_tree(self, widget, old_color: str, new_color: str) -> None:
        try:
            if str(widget.cget("bg")).lower() == old_color.lower():
                widget.configure(bg=new_color)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._recolor_tree(child, old_color, new_color)

    def _build_tabs(self) -> None:
        bar = tk.Frame(self.root, bg=self.bg_color)
        bar.pack(fill="x", padx=18)
        self._tab_buttons: dict[str, tk.Button] = {}
        for key, label in (("story", "Story"), ("towers", "Towers"), ("pvp", "PvP"), ("autoclicker", "Auto Clicker")):
            btn = tk.Button(
                bar, text=label, font=("Segoe UI", 10, "bold"),
                relief="flat", bd=0, width=14,
                command=lambda k=key: self._show_tab(k),
            )
            btn.pack(side="left", padx=(0, 4), fill="x", expand=True)
            self._tab_buttons[key] = btn

        self._content = tk.Frame(self.root, bg=self.bg_color)
        self._content.pack(fill="both", expand=True, padx=18, pady=(10, 0))

    def _show_tab(self, which: str) -> None:
        self._active_tab = which
        for key, btn in self._tab_buttons.items():
            btn.configure(bg=_TAB_ACTIVE if key == which else _TAB_INACTIVE,
                          fg=_FG if key == which else _MUTED)
        self._story_frame.pack_forget()
        self._towers_frame.pack_forget()
        self._pvp_frame.pack_forget()
        self._autoclicker_frame.pack_forget()
        frame = {
            "story": self._story_frame, "towers": self._towers_frame,
            "pvp": self._pvp_frame, "autoclicker": self._autoclicker_frame,
        }[which]
        frame.pack(fill="both", expand=True)

    # -- Story tab ----------------------------------------------------------------

    def _build_story_tab(self) -> None:
        frame = tk.Frame(self._content, bg=self.bg_color)
        self._story_frame = frame
        pad = {"padx": 0, "pady": 6}

        status_frame = tk.Frame(frame, bg=self.bg_color)
        status_frame.pack(**pad)
        self._story_dot = tk.Canvas(status_frame, width=14, height=14, bg=self.bg_color, highlightthickness=0)
        self._story_dot.pack(side="left", padx=(0, 8))
        self._story_dot_id = self._story_dot.create_oval(2, 2, 12, 12, fill=_ACCENT_STOPPED, outline="")
        self._story_status_label = tk.Label(
            status_frame, text="STOPPED", font=("Segoe UI", 13, "bold"), fg=_ACCENT_STOPPED, bg=self.bg_color
        )
        self._story_status_label.pack(side="left")

        btn_frame = tk.Frame(frame, bg=self.bg_color)
        btn_frame.pack(**pad)
        tk.Button(
            btn_frame, text="▶  Start", font=("Segoe UI", 11, "bold"),
            bg=_ACCENT_RUNNING, fg="#0a2e17", activebackground="#33c266",
            relief="flat", width=10, height=1, command=self._story_start,
        ).pack(side="left", padx=6)
        tk.Button(
            btn_frame, text="■  Stop", font=("Segoe UI", 11, "bold"),
            bg=_ACCENT_STOPPED, fg="#3a0a0a", activebackground="#e04444",
            relief="flat", width=10, height=1, command=self._story_stop,
        ).pack(side="left", padx=6)

        tk.Label(
            frame, text=f"Hotkeys: {self.start_key} start  ·  {self.stop_key} stop",
            font=("Segoe UI", 9), fg="#7a7a7a", bg=self.bg_color,
        ).pack(pady=(4, 2))
        tk.Label(
            frame, text=f"Clicks teleport-to-hero every {self.interval:g}s while running.",
            font=("Segoe UI", 9), fg="#7a7a7a", bg=self.bg_color,
        ).pack(pady=(0, 16))

    def _story_start(self) -> None:
        self.story_running_event.set()
        self.towers_running_event.clear()

    def _story_stop(self) -> None:
        self.story_running_event.clear()

    # -- Towers tab -----------------------------------------------------------------

    def _build_towers_tab(self) -> None:
        frame = tk.Frame(self._content, bg=self.bg_color)
        self._towers_frame = frame

        self._towers_indicator = tk.Label(
            frame, text="AUTOMATION STOPPED", font=("Segoe UI", 14, "bold"),
            fg=_ACCENT_STOPPED, bg=self.bg_color,
        )
        self._towers_indicator.pack(pady=(4, 10))

        btn_frame = tk.Frame(frame, bg=self.bg_color)
        btn_frame.pack(pady=4)
        tk.Button(
            btn_frame, text="▶  Start / Resume", font=("Segoe UI", 10, "bold"),
            bg=_ACCENT_RUNNING, fg="#0a2e17", activebackground="#33c266",
            relief="flat", width=14, command=self._towers_start,
        ).pack(side="left", padx=4)
        tk.Button(
            btn_frame, text="⏸  Pause", font=("Segoe UI", 10, "bold"),
            bg=_ACCENT_PAUSED, fg="#3a2600", activebackground="#e0a23f",
            relief="flat", width=10, command=self._towers_pause,
        ).pack(side="left", padx=4)
        tk.Button(
            btn_frame, text="⛔ Emergency Stop", font=("Segoe UI", 10, "bold"),
            bg=_ACCENT_STOPPED, fg="#3a0a0a", activebackground="#e04444",
            relief="flat", width=16, command=self._towers_estop,
        ).pack(side="left", padx=4)

        tk.Label(
            frame, text=f"Hotkey: {self.towers_key} = toggle enable/pause",
            font=("Segoe UI", 9), fg="#7a7a7a", bg=self.bg_color,
        ).pack(pady=(2, 10))

        # stat grid
        grid = tk.Frame(frame, bg=self.bg_color)
        grid.pack(pady=(0, 10), fill="x")
        self._stat_labels: dict[str, tk.Label] = {}
        stat_defs = [
            ("runtime", "Runtime"),
            ("doors_detected", "Doors Detected"),
            ("movements", "Movements"),
            ("teleports", "Teleports"),
            ("recoveries", "Recovery Events"),
        ]
        for i, (key, caption) in enumerate(stat_defs):
            cell = tk.Frame(grid, bg=_PANEL)
            cell.grid(row=i // 2, column=i % 2, padx=4, pady=4, sticky="ew")
            grid.grid_columnconfigure(i % 2, weight=1)
            val = tk.Label(cell, text="0", font=("Segoe UI", 15, "bold"), fg=_FG, bg=_PANEL)
            val.pack(pady=(6, 0))
            tk.Label(cell, text=caption, font=("Segoe UI", 8), fg=_MUTED, bg=_PANEL).pack(pady=(0, 6))
            self._stat_labels[key] = val

        tk.Label(frame, text="Activity Log", font=("Segoe UI", 9, "bold"), fg=_MUTED, bg=self.bg_color).pack(
            anchor="w", pady=(4, 2)
        )
        log_frame = tk.Frame(frame, bg=self.bg_color)
        log_frame.pack(fill="both", expand=True, pady=(0, 16))
        scrollbar = tk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")
        self._log_text = tk.Text(
            log_frame, height=10, width=44, bg="#111111", fg="#c8c8c8",
            font=("Consolas", 8), relief="flat", wrap="word",
            yscrollcommand=scrollbar.set, state="disabled",
        )
        self._log_text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self._log_text.yview)

    def _towers_start(self) -> None:
        self.towers_running_event.set()
        self.story_running_event.clear()

    def _towers_pause(self) -> None:
        self.towers_running_event.clear()

    def _towers_estop(self) -> None:
        self.towers_running_event.clear()
        if self.towers_automation is not None:
            self.towers_automation.navigator.stop_all_movement()
        self._append_log(self._log_text, "[INFO] Emergency stop — all inputs released")

    def _append_log(self, widget: tk.Text, line: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", line + "\n")
        widget.see("end")
        # cap log length so it doesn't grow forever over an overnight run
        if float(widget.index("end-1c").split(".")[0]) > 500:
            widget.delete("1.0", "2.0")
        widget.configure(state="disabled")

    # -- PvP tab --------------------------------------------------------------------

    def _build_pvp_tab(self) -> None:
        frame = tk.Frame(self._content, bg=self.bg_color)
        self._pvp_frame = frame

        self._pvp_indicator = tk.Label(
            frame, text="STOPPED", font=("Segoe UI", 14, "bold"),
            fg=_ACCENT_STOPPED, bg=self.bg_color,
        )
        self._pvp_indicator.pack(pady=(4, 10))

        btn_frame = tk.Frame(frame, bg=self.bg_color)
        btn_frame.pack(pady=4)
        tk.Button(
            btn_frame, text="▶  Start", font=("Segoe UI", 11, "bold"),
            bg=_ACCENT_RUNNING, fg="#0a2e17", activebackground="#33c266",
            relief="flat", width=10, command=self._pvp_start,
        ).pack(side="left", padx=6)
        tk.Button(
            btn_frame, text="■  Stop", font=("Segoe UI", 11, "bold"),
            bg=_ACCENT_STOPPED, fg="#3a0a0a", activebackground="#e04444",
            relief="flat", width=10, command=self._pvp_stop,
        ).pack(side="left", padx=6)

        tk.Label(
            frame, text=f"Hotkey: {self.pvp_key} = toggle",
            font=("Segoe UI", 9), fg="#7a7a7a", bg=self.bg_color,
        ).pack(pady=(4, 2))
        tk.Label(
            frame, text="Spam-clicks the Play Ranked button nonstop while running.",
            font=("Segoe UI", 9), fg="#7a7a7a", bg=self.bg_color,
        ).pack(pady=(0, 8))

        cell = tk.Frame(frame, bg=_PANEL)
        cell.pack(pady=(0, 10), fill="x")
        self._pvp_clicks_label = tk.Label(cell, text="0", font=("Segoe UI", 15, "bold"), fg=_FG, bg=_PANEL)
        self._pvp_clicks_label.pack(pady=(6, 0))
        tk.Label(cell, text="Clicks", font=("Segoe UI", 8), fg=_MUTED, bg=_PANEL).pack(pady=(0, 6))

        tk.Label(frame, text="Activity Log", font=("Segoe UI", 9, "bold"), fg=_MUTED, bg=self.bg_color).pack(
            anchor="w", pady=(4, 2)
        )
        log_frame = tk.Frame(frame, bg=self.bg_color)
        log_frame.pack(fill="both", expand=True, pady=(0, 16))
        scrollbar = tk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")
        self._pvp_log_text = tk.Text(
            log_frame, height=10, width=44, bg="#111111", fg="#c8c8c8",
            font=("Consolas", 8), relief="flat", wrap="word",
            yscrollcommand=scrollbar.set, state="disabled",
        )
        self._pvp_log_text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self._pvp_log_text.yview)

    def _pvp_start(self) -> None:
        if self.pvp_running_event is None:
            return
        self.pvp_running_event.set()
        self.story_running_event.clear()
        self.towers_running_event.clear()

    def _pvp_stop(self) -> None:
        if self.pvp_running_event is not None:
            self.pvp_running_event.clear()

    # -- Auto Clicker tab -------------------------------------------------------------

    def _build_auto_clicker_tab(self) -> None:
        frame = tk.Frame(self._content, bg=self.bg_color)
        self._autoclicker_frame = frame

        self._autoclicker_indicator = tk.Label(
            frame, text="STOPPED", font=("Segoe UI", 14, "bold"),
            fg=_ACCENT_STOPPED, bg=self.bg_color,
        )
        self._autoclicker_indicator.pack(pady=(4, 10))

        btn_frame = tk.Frame(frame, bg=self.bg_color)
        btn_frame.pack(pady=4)
        tk.Button(
            btn_frame, text="▶  Start", font=("Segoe UI", 11, "bold"),
            bg=_ACCENT_RUNNING, fg="#0a2e17", activebackground="#33c266",
            relief="flat", width=10, command=self._autoclicker_start,
        ).pack(side="left", padx=6)
        tk.Button(
            btn_frame, text="■  Stop", font=("Segoe UI", 11, "bold"),
            bg=_ACCENT_STOPPED, fg="#3a0a0a", activebackground="#e04444",
            relief="flat", width=10, command=self._autoclicker_stop,
        ).pack(side="left", padx=6)

        rebind_frame = tk.Frame(frame, bg=self.bg_color)
        rebind_frame.pack(pady=(6, 2))
        self._autoclicker_hotkey_label = tk.Label(
            rebind_frame, text=f"Hotkey: {self.auto_clicker_key}", font=("Segoe UI", 9), fg="#7a7a7a", bg=self.bg_color,
        )
        self._autoclicker_hotkey_label.pack(side="left", padx=(0, 8))
        self._autoclicker_rebind_btn = tk.Button(
            rebind_frame, text="Rebind", font=("Segoe UI", 8, "bold"),
            bg=_PANEL, fg=_FG, activebackground="#3a3a3a",
            relief="flat", width=8, command=self._autoclicker_start_rebind,
        )
        self._autoclicker_rebind_btn.pack(side="left")

        tk.Label(
            frame, text="Clicks at the current cursor position nonstop while running —\nmove the mouse where you want it clicking.",
            font=("Segoe UI", 9), fg="#7a7a7a", bg=self.bg_color, justify="center",
        ).pack(pady=(6, 8))

        cell = tk.Frame(frame, bg=_PANEL)
        cell.pack(pady=(0, 10), fill="x")
        self._autoclicker_clicks_label = tk.Label(cell, text="0", font=("Segoe UI", 15, "bold"), fg=_FG, bg=_PANEL)
        self._autoclicker_clicks_label.pack(pady=(6, 0))
        tk.Label(cell, text="Clicks", font=("Segoe UI", 8), fg=_MUTED, bg=_PANEL).pack(pady=(0, 6))

        tk.Label(frame, text="Activity Log", font=("Segoe UI", 9, "bold"), fg=_MUTED, bg=self.bg_color).pack(
            anchor="w", pady=(4, 2)
        )
        log_frame = tk.Frame(frame, bg=self.bg_color)
        log_frame.pack(fill="both", expand=True, pady=(0, 16))
        scrollbar = tk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")
        self._autoclicker_log_text = tk.Text(
            log_frame, height=10, width=44, bg="#111111", fg="#c8c8c8",
            font=("Consolas", 8), relief="flat", wrap="word",
            yscrollcommand=scrollbar.set, state="disabled",
        )
        self._autoclicker_log_text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self._autoclicker_log_text.yview)

    def _autoclicker_start(self) -> None:
        if self.auto_clicker_running_event is None:
            return
        self.auto_clicker_running_event.set()
        self.story_running_event.clear()
        self.towers_running_event.clear()

    def _autoclicker_stop(self) -> None:
        if self.auto_clicker_running_event is not None:
            self.auto_clicker_running_event.clear()

    def _autoclicker_start_rebind(self) -> None:
        if self._rebinding:
            return
        self._rebinding = True
        self._autoclicker_rebind_btn.configure(text="...", state="disabled")
        self._autoclicker_hotkey_label.configure(text="Press any key…")
        self.root.focus_force()
        self.root.bind_all("<KeyPress>", self._autoclicker_capture_rebind)

    def _autoclicker_capture_rebind(self, event) -> None:
        new_key = _tk_keysym_to_hotkey_name(event.keysym)
        if not new_key:
            # A bare modifier tap (Shift/Ctrl/Alt/...) isn't a usable
            # hotkey on its own — keep listening instead of canceling, so
            # an accidental modifier press doesn't force clicking Rebind
            # again.
            return

        self.root.unbind_all("<KeyPress>")
        self._rebinding = False
        self._autoclicker_rebind_btn.configure(text="Rebind", state="normal")

        self.auto_clicker_key = new_key
        self._autoclicker_hotkey_label.configure(text=f"Hotkey: {new_key}")
        if self.hotkey_listener is not None:
            self.hotkey_listener.auto_clicker_key = new_key.lower()
        self._save_config_value("hotkeys", "auto_clicker_toggle", new_key.lower())

    def _save_config_value(self, section: str, key: str, value) -> None:
        """Persists one config.yaml value via a round-trip parse (preserves
        every comment/blank-line/order in the file), same technique
        self_tuning.py uses for its own auto-adjustments."""
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
            pass  # rebind still works for this session even if the save fails

    # -- AFK banner (Toplevel, borderless, always-on-top) --------------------------

    def _build_banner(self) -> None:
        banner = tk.Toplevel(self.root)
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

        # Floor-number mirror: its own small overlay, bottom-right corner —
        # separate from the big centered banner text.
        floor_overlay = tk.Toplevel(self.root)
        self._floor_overlay = floor_overlay
        floor_overlay.overrideredirect(True)
        floor_overlay.attributes("-topmost", True)
        floor_overlay.configure(bg="black")
        floor_overlay.attributes("-transparentcolor", "black")
        self._floor_label = tk.Label(floor_overlay, bg="black")
        self._floor_label.pack(padx=10, pady=10)
        floor_overlay.withdraw()
        _make_click_through(floor_overlay)

    # -- AFK ELO FARMING overlay (PvP mode, top-middle) -----------------------------

    def _build_elo_overlay(self) -> None:
        overlay = tk.Toplevel(self.root)
        self._elo_overlay = overlay
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        # Solid blue panel (not colorkeyed transparent like the other
        # overlays) so it visually pops against the game instead of
        # blending into it.
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

    # -- Towers tracer overlay (full-screen, borderless, click-through) ------------

    def _build_tracer_overlay(self) -> None:
        tracer = tk.Toplevel(self.root)
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

    # -- shared poll loop -----------------------------------------------------------

    def _poll(self) -> None:
        story_on = self.story_running_event.is_set()
        towers_on = self.towers_running_event.is_set()
        pvp_on = self.pvp_running_event is not None and self.pvp_running_event.is_set()
        autoclicker_on = self.auto_clicker_running_event is not None and self.auto_clicker_running_event.is_set()

        color = _ACCENT_RUNNING if story_on else _ACCENT_STOPPED
        self._story_status_label.configure(text="RUNNING" if story_on else "STOPPED", fg=color)
        self._story_dot.itemconfig(self._story_dot_id, fill=color)

        t_color = _ACCENT_RUNNING if towers_on else _ACCENT_STOPPED
        self._towers_indicator.configure(
            text="AUTOMATION ENABLED" if towers_on else "AUTOMATION PAUSED", fg=t_color
        )

        if self.towers_automation is not None:
            s = self.towers_automation.stats
            self._stat_labels["doors_detected"].configure(text=str(s["doors_detected"]))
            self._stat_labels["movements"].configure(text=str(s["movements"]))
            self._stat_labels["teleports"].configure(text=str(s["teleports"]))
            self._stat_labels["recoveries"].configure(text=str(s["recoveries"]))
            runtime = self.towers_automation.runtime_seconds()
            mins, secs = divmod(int(runtime), 60)
            self._stat_labels["runtime"].configure(text=f"{mins:02d}:{secs:02d}")

        # drain the towers log queue (only touch Tk from this thread)
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(self._log_text, line)
        except queue.Empty:
            pass

        pvp_color = _ACCENT_RUNNING if pvp_on else _ACCENT_STOPPED
        self._pvp_indicator.configure(text="RUNNING" if pvp_on else "STOPPED", fg=pvp_color)
        if self.pvp_spam is not None:
            self._pvp_clicks_label.configure(text=str(self.pvp_spam.stats["clicks"]))
        if self.pvp_log_queue is not None:
            try:
                while True:
                    line = self.pvp_log_queue.get_nowait()
                    self._append_log(self._pvp_log_text, line)
            except queue.Empty:
                pass

        autoclicker_color = _ACCENT_RUNNING if autoclicker_on else _ACCENT_STOPPED
        self._autoclicker_indicator.configure(text="RUNNING" if autoclicker_on else "STOPPED", fg=autoclicker_color)
        if self.auto_clicker is not None:
            self._autoclicker_clicks_label.configure(text=str(self.auto_clicker.stats["clicks"]))
        if self.auto_clicker_log_queue is not None:
            try:
                while True:
                    line = self.auto_clicker_log_queue.get_nowait()
                    self._append_log(self._autoclicker_log_text, line)
            except queue.Empty:
                pass

        # The generic "AFK FARMING" banner is Story/Towers/Auto-Clicker —
        # PvP gets its own dedicated "AFK ELO FARMING" overlay below instead.
        running = story_on or towers_on or autoclicker_on
        # Don't fight with a hide_overlays_for_capture() in progress — the
        # capture Towers is taking right now needs these to stay hidden
        # until show_overlays_after_capture() explicitly restores them.
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

        # tracer overlay content: only meaningful during Towers, since it
        # draws DoorNavigator's own detection state
        if towers_on:
            self._draw_tracers()

        self.root.after(200, self._poll)

    # -- hide/show around a real detection screenshot -------------------------------
    # SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) would be the clean way
    # to keep our own windows out of our own screenshots, but it fails
    # outright on this system (confirmed — even resetting to WDA_NONE
    # fails). This is the guaranteed-correct fallback: physically hide every
    # overlay for the brief instant of the actual grab, then restore
    # immediately after. Callable from any thread (the Towers worker thread
    # calls this); Tk itself is only ever touched via root.after scheduling.

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

        self.root.after(0, _do)
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

        self.root.after(0, _do)

    def _handle_close(self) -> None:
        self.on_close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
