"""
hotkeys.py — global hotkey listener for both automation modes.

F6 / F7 control the "Story" teleport-only loop; F8 toggles "Towers" (press
once to enable, again to disable, again to resume from where it left off —
it's a plain toggle, the loop itself doesn't reset any state on pause).
Starting either mode stops the other — they'd otherwise fight over the same
clicks and movement keys.

Runs on its own thread via pynput so hotkeys work even when Roblox (not this
window) is focused. All this does is flip threading.Event flags — no click
or movement logic lives here.
"""

from __future__ import annotations

import logging
import threading

from pynput import keyboard


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
    ):
        hk = config["hotkeys"]
        self.start_key = _normalize(hk["start"])
        self.stop_key = _normalize(hk["stop"])
        self.towers_key = _normalize(hk.get("towers_toggle", "f8"))
        self.campaign_key = _normalize(hk.get("campaign_toggle", "f9"))
        self.pvp_key = _normalize(hk.get("pvp_toggle", "p"))

        self.story_running_event = story_running_event
        self.towers_running_event = towers_running_event
        self.campaign_running_event = campaign_running_event
        self.pvp_running_event = pvp_running_event
        self.logger = logger

        self._listener = keyboard.Listener(on_press=self._on_press)

    def _key_to_name(self, key) -> str | None:
        try:
            return key.char.lower()
        except AttributeError:
            name = str(key).replace("Key.", "")
            return name.lower()

    def _on_press(self, key) -> None:
        name = self._key_to_name(key)
        if name is None:
            return

        if name == self.start_key:
            if not self.story_running_event.is_set():
                self.logger.info("Start hotkey pressed — Story starting.")
            self.story_running_event.set()
            self.towers_running_event.clear()
            if self.campaign_running_event is not None:
                self.campaign_running_event.clear()
            if self.pvp_running_event is not None:
                self.pvp_running_event.clear()

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
                self.story_running_event.clear()
                if self.campaign_running_event is not None:
                    self.campaign_running_event.clear()
                if self.pvp_running_event is not None:
                    self.pvp_running_event.clear()

        elif name == self.campaign_key and self.campaign_running_event is not None:
            if self.campaign_running_event.is_set():
                self.logger.info("Campaign hotkey pressed — Story campaign stopping.")
                self.campaign_running_event.clear()
            else:
                self.logger.info("Campaign hotkey pressed — Story campaign starting.")
                self.campaign_running_event.set()
                self.story_running_event.clear()
                self.towers_running_event.clear()
                if self.pvp_running_event is not None:
                    self.pvp_running_event.clear()

        elif name == self.pvp_key and self.pvp_running_event is not None:
            if self.pvp_running_event.is_set():
                self.logger.info("PvP hotkey pressed — PvP spam-click stopping.")
                self.pvp_running_event.clear()
            else:
                self.logger.info("PvP hotkey pressed — PvP spam-click starting.")
                self.pvp_running_event.set()
                self.story_running_event.clear()
                self.towers_running_event.clear()
                if self.campaign_running_event is not None:
                    self.campaign_running_event.clear()

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()
