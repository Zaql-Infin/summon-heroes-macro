"""
hotkeys.py — global hotkey listener for all automation modes.

F6 / F7 control the "Story" teleport-only loop; F8 toggles "Towers" (press
once to enable, again to disable, again to resume from where it left off —
it's a plain toggle, the loop itself doesn't reset any state on pause).
Starting any one mode stops every other one — they'd otherwise fight over
the same clicks and movement keys.

Runs on its own thread via pynput so hotkeys work even when Roblox (not this
window) is focused — but only actually act while Roblox itself is the
focused window (hotkeys.require_roblox_focus, default true; see
input_sim.is_roblox_foreground), so e.g. typing "b" in some other app
doesn't accidentally toggle Campaign mode. All this does is flip
threading.Event flags — no click or movement logic lives here.

auto_clicker_key is rebindable live from the GUI's Auto Clicker tab (see
gui.py's ControlPanel.set_auto_clicker_key) — unlike the other hotkeys,
which are fixed for the life of the process, this one can change while
running, so it's read fresh on every keypress rather than cached like the
others would need extra plumbing for no real benefit.
"""

from __future__ import annotations

import logging
import threading

from pynput import keyboard

from . import input_sim


def _normalize(key_name: str) -> str:
    return key_name.lower().strip()


class HotkeyListener:
    def __init__(
        self,
        config: dict,
        story_running_event: threading.Event,
        towers_running_event: threading.Event,
        logger: logging.Logger,
        campaign_running_event: threading.Event | None = None,
        pvp_running_event: threading.Event | None = None,
        auto_clicker_running_event: threading.Event | None = None,
    ):
        hk = config["hotkeys"]
        self.start_key = _normalize(hk["start"])
        self.stop_key = _normalize(hk["stop"])
        self.towers_key = _normalize(hk.get("towers_toggle", "f8"))
        self.campaign_key = _normalize(hk.get("campaign_toggle", "f9"))
        self.pvp_key = _normalize(hk.get("pvp_toggle", "p"))
        self.auto_clicker_key = _normalize(hk.get("auto_clicker_toggle", "c"))

        # User-requested (2026-09-12): hotkeys used to fire no matter which
        # window had focus (the whole point of a "global" hotkey) — now
        # gated so they only act while Roblox itself is the focused window,
        # so e.g. typing "b" in Discord or a browser doesn't accidentally
        # toggle Campaign mode.
        self.require_roblox_focus = hk.get("require_roblox_focus", True)
        self.roblox_window_keyword = hk.get("roblox_window_keyword", "roblox")

        self.story_running_event = story_running_event
        self.towers_running_event = towers_running_event
        self.campaign_running_event = campaign_running_event
        self.pvp_running_event = pvp_running_event
        self.auto_clicker_running_event = auto_clicker_running_event
        self.logger = logger

        self._listener = keyboard.Listener(on_press=self._on_press)

    def _key_to_name(self, key) -> str | None:
        try:
            return key.char.lower()
        except AttributeError:
            name = str(key).replace("Key.", "")
            return name.lower()

    def _stop_all_except(self, keep: threading.Event | None) -> None:
        for ev in (
            self.story_running_event,
            self.towers_running_event,
            self.campaign_running_event,
            self.pvp_running_event,
            self.auto_clicker_running_event,
        ):
            if ev is not None and ev is not keep:
                ev.clear()

    def _on_press(self, key) -> None:
        name = self._key_to_name(key)
        if name is None:
            return

        if self.require_roblox_focus and not input_sim.is_roblox_foreground(self.roblox_window_keyword):
            return

        if name == self.start_key:
            if not self.story_running_event.is_set():
                self.logger.info("Start hotkey pressed — Story starting.")
            self.story_running_event.set()
            self._stop_all_except(self.story_running_event)

        elif name == self.stop_key:
            if self.story_running_event.is_set():
                self.logger.info("Stop hotkey pressed — Story stopping.")
            self.story_running_event.clear()

        elif name == self.towers_key:
            if self.towers_running_event.is_set():
                self.logger.info("Towers hotkey pressed — Towers stopping.")
                self.towers_running_event.clear()
            else:
                self.logger.info("Towers hotkey pressed — Towers starting.")
                self.towers_running_event.set()
                self._stop_all_except(self.towers_running_event)

        elif name == self.campaign_key and self.campaign_running_event is not None:
            if self.campaign_running_event.is_set():
                self.logger.info("Campaign hotkey pressed — Story campaign stopping.")
                self.campaign_running_event.clear()
            else:
                self.logger.info("Campaign hotkey pressed — Story campaign starting.")
                self.campaign_running_event.set()
                self._stop_all_except(self.campaign_running_event)

        elif name == self.pvp_key and self.pvp_running_event is not None:
            if self.pvp_running_event.is_set():
                self.logger.info("PvP hotkey pressed — PvP spam-click stopping.")
                self.pvp_running_event.clear()
            else:
                self.logger.info("PvP hotkey pressed — PvP spam-click starting.")
                self.pvp_running_event.set()
                self._stop_all_except(self.pvp_running_event)

        elif name == self.auto_clicker_key and self.auto_clicker_running_event is not None:
            if self.auto_clicker_running_event.is_set():
                self.logger.info("Auto Clicker hotkey pressed — Auto Clicker stopping.")
                self.auto_clicker_running_event.clear()
            else:
                self.logger.info("Auto Clicker hotkey pressed — Auto Clicker starting.")
                self.auto_clicker_running_event.set()
                self._stop_all_except(self.auto_clicker_running_event)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()
