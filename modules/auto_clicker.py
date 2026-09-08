"""
auto_clicker.py — spam-clicks wherever the cursor currently is, nonstop,
while enabled. A generic auto-clicker, unlike pvp_spam.py (which clicks one
fixed screen coordinate) — this one never moves the mouse at all, it just
fires left-clicks at the cursor's current position as fast as reliably
possible.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from pynput.mouse import Controller, Button

_mouse = Controller()


class AutoClicker:
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

        ac_cfg = config.get("auto_clicker", {})
        # 0.01s -> up to ~100 clicks/sec in principle; real-world throughput
        # is lower once Python/pynput call overhead is accounted for, but
        # this is as fast as this approach can reliably push without
        # skipping the actual click() call that makes each one register.
        self.interval_seconds = ac_cfg.get("interval_seconds", 0.01)

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
                        self._log(f"[AUTOCLICK] Stopped — {self.stats['clicks']} clicks this run.")
                        was_running = False
                    time.sleep(0.05)
                    continue
                if not was_running:
                    self._log("[AUTOCLICK] Started — clicking at cursor position nonstop.")
                    was_running = True
                _mouse.click(Button.left, 1)
                self.stats["clicks"] += 1
                time.sleep(self.interval_seconds)
        except Exception as e:
            self.logger.error("Auto-clicker loop crashed: %s", e)
            self._log(f"[ERROR] {e}")

    def stop(self) -> None:
        self._shutdown_flag.set()
