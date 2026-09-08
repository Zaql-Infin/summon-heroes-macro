"""
towers.py — the "Towers" automation mode.

Cycle: teleport-to-hero while no door is visible -> the moment a door
appears, stop teleporting, pick the best-ranked one, walk into it -> repeat.
Runs as its own thread, toggled by a dedicated running_event (F8), separate
from the simple "Story" teleport-only loop. Reports stats and human-readable
activity lines through log_callback for the GUI's activity log.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

import cv2

from . import navigation, vision

_DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug")


class TowersAutomation:
    def __init__(
        self,
        config: dict,
        capture: vision.ScreenCapture,
        teleport_fn: Callable[[], None],
        running_event: threading.Event,
        logger: logging.Logger,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.cfg = config
        self.capture = capture
        self.teleport_fn = teleport_fn
        self.running_event = running_event
        self.logger = logger
        self.log_callback = log_callback

        self.navigator = navigation.DoorNavigator(config, capture, logger)
        self._shutdown_flag = threading.Event()

        self.stats = {"doors_detected": 0, "movements": 0, "teleports": 0, "recoveries": 0}
        self.start_time: Optional[float] = None

        # Wired up by main.py after the GUI is constructed. Hides our own
        # overlay windows (banner, floor mirror, tracer boxes) for the
        # instant of the actual screen grab so our own screenshot-based
        # detection can never see them — see gui.py's hide/show methods for
        # why this exists instead of the (broken, on this system)
        # SetWindowDisplayAffinity approach.
        self.hide_overlays_fn: Optional[Callable[[], None]] = None
        self.show_overlays_fn: Optional[Callable[[], None]] = None

    def _log(self, msg: str) -> None:
        self.logger.info(msg)
        if self.log_callback:
            self.log_callback(msg)

    def _save_debug_screenshot(self, frame) -> None:
        """Saves an annotated snapshot (all candidates boxed, chosen one in
        green) whenever a door is detected — a static record of exactly
        what was seen, for reviewing without needing to watch it live."""
        try:
            os.makedirs(_DEBUG_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S_") + f"{int(time.time() * 1000) % 1000:03d}"
            path = os.path.join(_DEBUG_DIR, f"detect_{ts}.png")
            self.navigator.save_debug_image(frame, path)
            self._log(f"[VISION] Debug screenshot saved: debug/{os.path.basename(path)}")
        except Exception as e:
            self.logger.error("Failed to save debug screenshot: %s", e)

    def _grab_frame(self):
        """capture.grab() with the macro's own overlay windows hidden for
        the instant of the screenshot — see hide_overlays_fn above. The
        tracer overlay is full-screen, so ANY capture used for detection or
        OCR needs this, not just the main door-detection one."""
        if self.hide_overlays_fn:
            self.hide_overlays_fn()
        try:
            return self.capture.grab()
        finally:
            if self.show_overlays_fn:
                self.show_overlays_fn()

    def _check_for_door(self):
        """Grabs a frame, runs detection, and saves a debug screenshot if a
        door was found. Returns the match (or None)."""
        frame = self._grab_frame()
        match = self.navigator.detect_door(frame)
        if match is not None:
            self._save_debug_screenshot(frame)
        return match

    def _wait_for_floor_cleared(self) -> None:
        """Blocks (with a timeout) until the "Floor Cleared" banner is
        detected — a door can render in the background before the encounter
        is actually finished, so this confirms it before we walk. If no
        floor_cleared template is configured, is_floor_cleared() always
        returns True and this returns immediately (gate skipped, not
        blocking)."""
        timeout = self.cfg.get("floor_cleared", {}).get("wait_timeout_seconds", 12.0)
        self._log("[INFO] Waiting for floor cleared confirmation")
        start = time.time()
        last_diag = "<none seen>"
        best_frame = None
        while time.time() - start < timeout:
            if self._shutdown_flag.is_set() or not self.running_event.is_set():
                return
            frame = self._grab_frame()
            cleared, diag = self.navigator.floor_cleared_status(frame)
            last_diag, best_frame = diag, frame
            if cleared:
                self._log(f"[INFO] Floor cleared confirmed ({diag})")
                return
            time.sleep(0.3)

        self._log(
            f"[RECOVERY] Floor-cleared confirmation timed out after {timeout:g}s "
            f"(last seen: {last_diag}) — proceeding anyway"
        )
        if best_frame is not None:
            try:
                os.makedirs(_DEBUG_DIR, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S_") + f"{int(time.time() * 1000) % 1000:03d}"
                path = os.path.join(_DEBUG_DIR, f"floorcleared_miss_{ts}.png")
                cv2.imwrite(path, best_frame)
                self._log(f"[VISION] Best floor-cleared attempt saved: debug/{os.path.basename(path)}")
            except Exception as e:
                self.logger.error("Failed to save floor-cleared debug screenshot: %s", e)

    def _teleport_until_doors_appear(self):
        tp_cfg = self.cfg["teleport_to_hero"]
        teleport_enabled = tp_cfg.get("enabled", True)
        interval = tp_cfg.get("interval_seconds", 2.5)
        # Every loop cycle checks for a door AND paces itself at this
        # interval (replacing a separate tight poll + independent rate
        # limit) — simpler, and fixes a real regression the old two-timer
        # version had: a door check could get skipped on a given tick while
        # the teleport timer independently fired anyway, teleporting right
        # past a door that was actually already visible that same instant.
        # Under door_detection.method: "ai_hybrid" each check is a real,
        # billed API call, so this also keeps call volume sane during a
        # long fight without ever skipping the check that actually matters.
        check_interval = tp_cfg.get("door_check_min_interval_seconds", 1.5)
        last_action = time.time()
        announced_search = False
        search_start = time.time()
        stuck_warned = False
        # A long boss fight can legitimately take several minutes with zero
        # doors visible — this isn't itself a failure, so no disruptive
        # recovery action is taken. But without any signal at all, a truly
        # stuck state (e.g. detection silently broken) would look identical
        # to "still fighting" for the rest of the night. One clear log line
        # once this stretch gets unreasonably long gives that visibility.
        stuck_threshold_seconds = 180.0

        while True:
            if self._shutdown_flag.is_set() or not self.running_event.is_set():
                return None

            if not announced_search:
                self._log("[VISION] Searching for available routes")
                announced_search = True

            if not stuck_warned and time.time() - search_start > stuck_threshold_seconds:
                self._log(
                    f"[RECOVERY] No door detected in over {stuck_threshold_seconds:g}s — "
                    "still waiting (a genuinely long fight looks the same as a stuck "
                    "detection from here, so check in if this keeps going)."
                )
                stuck_warned = True

            # Always check for a door — every single cycle, never skipped.
            match = self._check_for_door()
            if match is not None:
                return match

            now = time.time()
            if now - last_action >= interval:
                last_action = now
                if teleport_enabled:
                    self._log("[VISION] Searching for teleport control")
                    try:
                        self.teleport_fn()
                        self.stats["teleports"] += 1
                        self._log("[ACTION] Teleport control activated")
                    except Exception as e:
                        self.logger.error("Teleport action failed: %s", e)
                        self.stats["recoveries"] += 1
                        self._log(f"[RECOVERY] Teleport failed, retrying: {e}")
                    # Teleporting moves the camera — give it a moment to
                    # settle before trusting any door detection off frame.
                    time.sleep(tp_cfg.get("camera_settle_seconds", 0.8))
                    match = self._check_for_door()
                    if match is not None:
                        return match

                # Camera-sweep look-around — runs on this cadence
                # regardless of whether teleporting is enabled, so a search
                # still actively looks around even with teleport off.
                # Bounded to once per cycle (not continuous), so a long
                # boss fight doesn't turn into wandering in circles.
                self.navigator.scan_tick()

            time.sleep(check_interval)

    def run(self) -> None:
        self.start_time = time.time()
        self._log("[INFO] Towers automation enabled")
        consecutive_failures = 0

        try:
            while not self._shutdown_flag.is_set():
                if not self.running_event.is_set():
                    time.sleep(0.1)
                    continue

                try:
                    match = self._teleport_until_doors_appear()
                    if match is None:
                        continue  # stopped/paused mid-wait

                    self.stats["doors_detected"] += 1
                    name = self.navigator.last_match_name
                    self._log(f"[VISION] Door detected: {name}")
                    self._log(f"[NAVIGATION] Selected target: {name}")

                    # A door can be visible in the background before the
                    # encounter is actually finished — wait for the "Floor
                    # Cleared" confirmation before walking, not just
                    # detection.
                    self._wait_for_floor_cleared()

                    # Re-locate right before walking — the door's screen
                    # position may have drifted while waiting. This must be
                    # a cheap LOCAL re-check for the type already chosen
                    # (locate_type), never a full detect_door() re-run:
                    # under door_detection.method "ai_hybrid", detect_door()
                    # fires a fresh AI call that can — and was observed to,
                    # live — second-guess the original decision and hand
                    # back a totally different door type mid-navigation
                    # (picked "chest", confirmed floor-cleared for chest,
                    # then the alignment log showed it turning toward
                    # "elite" instead — the target silently switched).
                    # Re-checking the position of the type we already
                    # committed to can't do that.
                    frame = self._grab_frame()
                    fresh_match = self.navigator.locate_type(frame, name, near_x=match.center[0])
                    if fresh_match is not None:
                        walk_target = fresh_match
                        self.navigator.last_match_name = name
                        self.navigator.last_chosen_match = fresh_match
                    else:
                        walk_target = match
                        self.navigator.last_match_name = name

                    self._log("[NAVIGATION] Moving toward target")
                    self.navigator.approach_and_enter(walk_target)
                    self.stats["movements"] += 1
                    self._log("[ACTION] Jump action triggered")
                    self._log("[INFO] Waiting for game state update")
                    consecutive_failures = 0

                except Exception as e:
                    # Anything above can throw for reasons that have
                    # nothing to do with a bug (a transient screen-capture
                    # hiccup, OCR briefly unavailable, a frame grabbed
                    # mid-alt-tab) — for an unattended overnight run, one
                    # unlucky exception must never be allowed to silently
                    # end the whole session. Caught per-iteration (not just
                    # around approach_and_enter like before) so a failure
                    # anywhere in the cycle logs, releases any held keys,
                    # and moves on to the next attempt instead of falling
                    # through to the outer handler and terminating the
                    # thread for good.
                    consecutive_failures += 1
                    self.logger.error("Towers cycle failed (%d in a row): %s", consecutive_failures, e)
                    self.stats["recoveries"] += 1
                    self._log(f"[RECOVERY] Cycle failed, resetting and retrying: {e}")
                    self.navigator.stop_all_movement()
                    # Back off a bit longer after repeated failures so a
                    # persistent problem (e.g. Tesseract genuinely missing)
                    # spins slowly instead of burning CPU and flooding the
                    # log every millisecond.
                    time.sleep(min(1.0 * consecutive_failures, 10.0))

        except Exception as e:  # last-resort safety net — never leave keys held
            self.logger.error("Towers automation crashed: %s", e)
            self._log(f"[ERROR] {e}")
        finally:
            self.navigator.stop_all_movement()

    def stop(self) -> None:
        self._shutdown_flag.set()
        self.navigator.stop_all_movement()

    def runtime_seconds(self) -> float:
        if self.start_time is None or not self.running_event.is_set():
            return 0.0
        return time.time() - self.start_time
