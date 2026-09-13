"""
skip_farm.py — the "Skip" automation mode (2026-09-13, user-requested).

A dedicated, standalone mode — its own hotkey, own GUI tab, own running
thread — completely independent of Towers' multi-door-type priority system.
It only ever looks for ONE door type, "skip", and does nothing else:

  1. Teleport-to-hero on a timer while searching for the Skip door
     specifically (never any other door type).
  2. The moment it's found, walk to/through it — reuses the exact same
     click-to-move approach_and_enter logic (camera-align, click-to-move,
     retry, walk-to-center) already proven for Towers mode, via a second
     DoorNavigator instance configured to only ever load the "skip"
     template. No door-handling code is duplicated here.
  3. Once through, spam the teleport-to-hero button every
     skip_farm.farm_teleport_interval_seconds (default 3.0) in a loop.
  4. The whole time, keep periodically re-checking whether the Skip door
     has become visible again — the moment it has (e.g. after dying and
     respawning back where it's visible, or after that room's cycle
     finishes), stop spamming and walk to it again. This is what makes
     "when I die, do it again" work without any explicit death-detection:
     dying+respawning is indistinguishable, from this loop's perspective,
     from "the Skip door is visible again, go interact with it."
  5. Repeat forever until running_event is cleared (hotkey toggle off).

There's no shipped template for the "skip" door type — nobody involved in
building this has seen what it looks like in-game. Capture it yourself:
    python tools/capture_template.py templates/door_archway_skip.png
while a Skip door is visible, then point skip_farm.template_path at it in
config.yaml (already defaults to that same path).
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Callable, Optional

from . import input_sim, navigation, vision


class SkipFarm:
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
        self._shutdown_flag = threading.Event()

        sf_cfg = config.get("skip_farm", {})

        # A second, independent DoorNavigator scoped to ONLY the "skip"
        # door type — reuses the exact same click-to-move
        # approach/align/retry logic already proven for Towers mode
        # (see navigation.py), without touching Towers' own navigator
        # instance or its multi-type priority ranking at all. Deep-copied
        # config so mutating door_detection here can't affect the real
        # Towers navigator, which is a completely separate instance
        # reading the original config dict.
        skip_nav_cfg = copy.deepcopy(config)
        skip_nav_cfg["door_detection"] = dict(config.get("door_detection", {}))
        skip_nav_cfg["door_detection"]["templates"] = [
            {"name": "skip", "path": sf_cfg.get("template_path", "templates/door_archway_skip.png")}
        ]
        skip_nav_cfg["door_detection"]["priority"] = ["skip"]
        # Always plain template matching for this dedicated single-type
        # search — ai_hybrid/yolo both exist to resolve cross-type
        # confusion among several candidate doors, which can't happen here
        # since exactly one type is ever loaded.
        skip_nav_cfg["door_detection"]["method"] = "template"
        skip_nav_cfg["door_detection"]["match_threshold"] = sf_cfg.get("match_threshold", 0.75)
        if sf_cfg.get("search_region") is not None:
            skip_nav_cfg["door_detection"]["search_region"] = sf_cfg["search_region"]
        self.navigator = navigation.DoorNavigator(skip_nav_cfg, capture, logger)

        self.teleport_interval_seconds = sf_cfg.get("teleport_interval_seconds", 1.5)
        self.door_check_interval_seconds = sf_cfg.get("door_check_interval_seconds", 0.75)
        self.farm_teleport_interval_seconds = sf_cfg.get("farm_teleport_interval_seconds", 1.5)
        self.farm_door_check_interval_seconds = sf_cfg.get("farm_door_check_interval_seconds", 1.0)
        # User-requested (2026-09-13): after entering the door, jump this
        # many times (1s apart) before settling into the teleport-farm
        # loop. Skip-mode-only — doesn't touch Towers' shared
        # approach_and_enter, which still just jumps once.
        self.post_entry_jump_count = sf_cfg.get("post_entry_jump_count", 5)
        self.post_entry_jump_interval_seconds = sf_cfg.get("post_entry_jump_interval_seconds", 1.0)

        # User-requested (2026-09-13): a "TOWER RUN ENDED!" results banner
        # appears after dying — pause everything for run_ended_pause_seconds
        # while it's up rather than teleporting/scanning through it. OCR-
        # based (like boss_floor's "BOSS" check) rather than a template —
        # this banner's text looks like a clean, plain font in the
        # screenshot it was reported from, not the game's usual bubble
        # font Tesseract struggles with elsewhere in this project. No
        # region restriction by default (checks the full frame) since its
        # exact on-screen position/resolution wasn't known when this was
        # written — narrow it with run_ended_ocr_region if it turns out
        # to be slow or to false-match something else.
        self.run_ended_keyword = sf_cfg.get("run_ended_keyword", "RUN ENDED")
        self.run_ended_ocr_region = sf_cfg.get("run_ended_ocr_region")
        self.run_ended_pause_seconds = sf_cfg.get("run_ended_pause_seconds", 10.0)

        # User-requested (2026-09-13): zoom the camera out once every time
        # a search cycle starts again (i.e. every time round the main loop
        # in run() — after entering a door, farming, and coming back
        # around to search for the next one, which is also what happens
        # after dying and respawning). Negative clicks = scroll down =
        # zoom out, the opposite direction from _ensure_first_person's
        # one-time zoom-in.
        self.zoom_out_clicks_on_restart = sf_cfg.get("zoom_out_clicks_on_restart", 1)

        # User-requested (2026-09-13): the full-frame run-ended OCR check
        # (above) was being run on EVERY single poll of _check_for_skip_door
        # — called multiple times per loop iteration in both the search and
        # farm loops — and full-frame OCR with no region restriction costs
        # far more than the ~0.4s a small HUD-region OCR call costs
        # elsewhere in this project (boss_floor). That overhead was silently
        # stacking on top of every configured interval, stretching what
        # should've been a ~1.5s teleport cadence out to ~7s+ in practice
        # (confirmed from live logs). Throttled so the expensive OCR call
        # only actually runs once every run_ended_check_interval_seconds;
        # every poll in between just does the cheap template-match door
        # check instead.
        self.run_ended_check_interval_seconds = sf_cfg.get("run_ended_check_interval_seconds", 2.0)
        self._last_run_ended_check = 0.0

        self.stats = {"doors_entered": 0, "farm_teleports": 0, "recoveries": 0}
        self.start_time: Optional[float] = None

        # Wired up by main.py after the GUI is constructed, same pattern as
        # towers.py's own hide/show hooks.
        self.hide_overlays_fn: Optional[Callable[[], None]] = None
        self.show_overlays_fn: Optional[Callable[[], None]] = None

    def _zoom_out_once(self) -> None:
        if not self.zoom_out_clicks_on_restart:
            return
        cx = self.navigator.frame_w // 2
        cy = int(self.navigator.frame_h * 0.4)
        input_sim.scroll_at(cx, cy, -abs(self.zoom_out_clicks_on_restart))
        self._log("[ACTION] Zoomed camera out")

    def _log(self, msg: str) -> None:
        self.logger.info(msg)
        if self.log_callback:
            self.log_callback(msg)

    def _grab_frame(self):
        if self.hide_overlays_fn:
            self.hide_overlays_fn()
        try:
            return self.capture.grab()
        finally:
            if self.show_overlays_fn:
                self.show_overlays_fn()

    def _is_run_ended_screen(self, frame) -> bool:
        try:
            text = vision.ocr_text(frame, region=self.run_ended_ocr_region).strip()
            return self.run_ended_keyword.upper() in text.upper()
        except Exception:
            return False

    def _check_for_skip_door(self):
        """Grabs one frame and checks it for both the "TOWER RUN ENDED!"
        results banner and the Skip door itself — sharing the one capture
        rather than grabbing twice. If the banner's up, pauses everything
        right here for run_ended_pause_seconds (the caller just sees "no
        door found this check", same as if it simply weren't visible yet)
        instead of teleporting/scanning through it. User-requested
        (2026-09-13): once the pause is over, zoom the camera out once so
        the respawned view can actually see the doors — respawning after
        the run-ended screen tends to land zoomed in too close to spot
        them.

        The OCR check itself is throttled to run_ended_check_interval_seconds
        (see __init__) — it's far more expensive than the template-match
        door check, and running it on every single poll was the main thing
        making this mode slow overall (confirmed from live logs: ~7s
        between teleports instead of the configured ~1.5s)."""
        frame = self._grab_frame()
        now = time.time()
        if now - self._last_run_ended_check >= self.run_ended_check_interval_seconds:
            self._last_run_ended_check = now
            if self._is_run_ended_screen(frame):
                self._log(f"[SKIP] Tower run ended screen detected — pausing {self.run_ended_pause_seconds:g}s")
                time.sleep(self.run_ended_pause_seconds)
                self._zoom_out_once()
                return None
        return self.navigator.detect_door(frame)

    def _search_for_skip_door(self):
        """Teleport-to-hero on a timer while searching, same pattern as
        towers.py's _teleport_until_doors_appear — just scoped to the one
        door type this navigator ever loads. Returns the match, or None if
        stopped/paused mid-search.

        Bug fixed 2026-09-13 (user-reported: after dying and respawning,
        it "kept turning/teleporting instead of going to the skip door"
        even once it was visible): this used to call scan_tick()
        immediately after teleport_fn(), stacking camera motion from the
        scan sweep on top of a camera that hadn't settled from the
        teleport yet — every frame grabbed during that window is a
        mid-transition frame, which is exactly why towers.py's own search
        loop waits camera_settle_seconds after teleporting and does one
        settled re-check BEFORE ever calling scan_tick(). This loop was
        missing both of those and went straight to scanning on an
        unsettled view, which explains match flickering in and out
        instead of ever being confidently locked onto."""
        self._zoom_out_once()

        last_action = time.time()
        announced = False
        camera_settle_seconds = self.cfg.get("teleport_to_hero", {}).get("camera_settle_seconds", 0.8)
        while True:
            if self._shutdown_flag.is_set() or not self.running_event.is_set():
                return None
            if not announced:
                self._log("[SKIP] Searching for Skip door")
                announced = True

            match = self._check_for_skip_door()
            if match is not None:
                return match

            now = time.time()
            if now - last_action >= self.teleport_interval_seconds:
                last_action = now
                try:
                    self.teleport_fn()
                    self._log("[ACTION] Teleport control activated")
                except Exception as e:
                    self.logger.error("Skip farm teleport failed: %s", e)
                    self._log(f"[RECOVERY] Teleport failed, retrying: {e}")

                # Let the camera actually settle from the teleport before
                # trusting anything grabbed from it, then check once on
                # that now-settled frame before ever starting a scan sweep.
                time.sleep(camera_settle_seconds)
                match = self._check_for_skip_door()
                if match is not None:
                    return match

                self.navigator.scan_tick()

            time.sleep(self.door_check_interval_seconds)

    def _farm_until_skip_reappears(self) -> None:
        """Spams the teleport-to-hero button on a timer, forever, until
        either the Skip door becomes visible again (walk to it again — see
        module docstring for why this also covers "died and respawned") or
        the mode gets toggled off.

        Bug fixed 2026-09-13 (user-reported: after dying it "just didn't
        even try find the doors", racking up 179 farm teleports with no
        further progress): this used to just spam teleport-to-hero and
        check whatever was ALREADY in view — teleporting recenters
        position, not camera rotation, so if you respawn facing a
        different direction than the doors, nothing here ever turned the
        camera to look around, and it would sit there farming forever no
        matter how long you waited. Now does the same
        settle-then-recheck-then-scan sequence _search_for_skip_door
        uses, on every teleport cycle, so it actively keeps looking
        around instead of passively hoping the door is already in view."""
        last_teleport = 0.0
        camera_settle_seconds = self.cfg.get("teleport_to_hero", {}).get("camera_settle_seconds", 0.8)
        while True:
            if self._shutdown_flag.is_set() or not self.running_event.is_set():
                return

            match = self._check_for_skip_door()
            if match is not None:
                self._log("[SKIP] Door visible again — walking to it")
                return

            now = time.time()
            if now - last_teleport >= self.farm_teleport_interval_seconds:
                last_teleport = now
                try:
                    self.teleport_fn()
                    self.stats["farm_teleports"] += 1
                except Exception as e:
                    self.logger.error("Skip farm teleport failed: %s", e)
                    self._log(f"[RECOVERY] Teleport failed, retrying: {e}")

                time.sleep(camera_settle_seconds)
                match = self._check_for_skip_door()
                if match is not None:
                    self._log("[SKIP] Door visible again — walking to it")
                    return

                self.navigator.scan_tick()
                match = self._check_for_skip_door()
                if match is not None:
                    self._log("[SKIP] Door visible again — walking to it")
                    return

            time.sleep(min(self.farm_door_check_interval_seconds, self.farm_teleport_interval_seconds))

    def run(self) -> None:
        self.start_time = time.time()
        self._log("[INFO] Skip farm enabled")
        consecutive_failures = 0

        try:
            while not self._shutdown_flag.is_set():
                if not self.running_event.is_set():
                    time.sleep(0.1)
                    continue

                try:
                    match = self._search_for_skip_door()
                    if match is None:
                        continue  # stopped/paused mid-search

                    self._log("[NAVIGATION] Skip door found — walking to it")
                    self.navigator.approach_and_enter(match)
                    self.stats["doors_entered"] += 1
                    self._log("[ACTION] Skip door entered")

                    # User-requested (2026-09-13): jump a few times, 1s
                    # apart, before settling into the teleport-farm loop.
                    if self.navigator.jump_key and self.post_entry_jump_count > 0:
                        self._log(f"[ACTION] Jumping {self.post_entry_jump_count} times")
                        for _ in range(self.post_entry_jump_count):
                            if self._shutdown_flag.is_set() or not self.running_event.is_set():
                                break
                            input_sim.tap_key(self.navigator.jump_key, self.navigator.jump_hold_seconds)
                            time.sleep(self.post_entry_jump_interval_seconds)

                    self._farm_until_skip_reappears()
                    consecutive_failures = 0

                except Exception as e:
                    # Same reasoning as towers.py: an unattended overnight
                    # run can't let one unlucky exception silently end the
                    # whole session.
                    consecutive_failures += 1
                    self.logger.error("Skip farm cycle failed (%d in a row): %s", consecutive_failures, e)
                    self.stats["recoveries"] += 1
                    self._log(f"[RECOVERY] Cycle failed, resetting and retrying: {e}")
                    self.navigator.stop_all_movement()
                    time.sleep(min(1.0 * consecutive_failures, 10.0))

        except Exception as e:
            self.logger.error("Skip farm crashed: %s", e)
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
