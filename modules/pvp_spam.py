"""
pvp_spam.py — spam-clicks the "Play Ranked" button nonstop while enabled.

Deliberately the simplest mode in the app: no screen reading, no detection,
just click_coordinate over and over on a fast fixed interval for as long as
running_event is set. Toggled on/off via a hotkey (see hotkeys.py), same
pattern as Story campaign's B toggle.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import input_sim


class PvpSpam:
    def __init__(
        self,
        config: dict,
        running_event: threading.Event,
        logger: logging.Logger,
        log_callback: Optional[callable] = None,
    ):
        self.running_event = running_event
        self.logger = logger
        self.log_callback = log_callback
        self._shutdown_flag = threading.Event()
        self.stats = {"clicks": 0}

        pvp_cfg = config.get("pvp_spam", {})
        coord = pvp_cfg.get("click_coordinate", [200, 293])
        self.click_coordinate = (coord[0], coord[1])
        # Fast enough to read as "nonstop spam" without pegging a CPU core —
        # the click itself (input_sim.click_at) already costs a bit more
        # than this sleep on the very first click (settling the cursor onto
        # the button), then stays cheap every call after since the cursor is
        # already there.
        self.interval_seconds = pvp_cfg.get("interval_seconds", 0.05)

    def _log(self, msg: str) -> None:
        self.logger.info(msg)
        if self.log_callback:
            self.log_callback(msg)

    def run(self) -> None:
        was_running = False
        try:
            while not self._shutdown_flag.is_set():
                if not self.running_event.is_set():
                    if was_running:
                        self._log(f"[PVP] Stopped — {self.stats['clicks']} clicks this run.")
                        was_running = False
                    time.sleep(0.1)
                    continue
                if not was_running:
                    self._log(f"[PVP] Started — spam-clicking {self.click_coordinate} nonstop.")
                    was_running = True
                input_sim.click_at(*self.click_coordinate)
                self.stats["clicks"] += 1
                time.sleep(self.interval_seconds)
        except Exception as e:
            self.logger.error("PvP spam-click loop crashed: %s", e)
            self._log(f"[ERROR] {e}")

    def stop(self) -> None:
        self._shutdown_flag.set()
