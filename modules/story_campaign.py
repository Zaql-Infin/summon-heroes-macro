"""
story_campaign.py — runs the Story mode campaign start-to-finish on loop.

How the game actually behaves (per user description + live confirmation,
2026-09-08): pressing Start on Rookie Island Stage 1 lets the game
auto-progress through every subsequent stage and island entirely on its
own — "it just keeps on going till it can't go anymore." When it truly
can't go further (finishing Memory's End, the last stage), a standalone
"VICTORY!" results screen appears with a "Back To Lobby" button — that's
the actual completion signal, not anything inside the Story menu itself
(an earlier theory — matching a Stargazer boss-card template inside the
menu — was wrong, corrected once the real screen was actually observed).
So this mode's job is small: open the Story menu, navigate to Rookie
Island Stage 1, click Start once, then just watch for that VICTORY screen
— and when it appears, click Back To Lobby, walk back into the portal,
and do it all again.

Coordinates below were calibrated against live 2560x1440 captures of the
actual Story menu and VICTORY screen (2026-09-08) — will need
recalibrating with tools/capture_template.py if the UI ever changes
layout.

Resolution support: originally tried a single uniform scale factor
(actual_width / 2560) applied to every coordinate, on the assumption
Roblox's UI is a straight proportional scale of the whole screen. Proven
wrong live (2026-09-08): calibrating fresh against a real 1920x1080
capture, each button's actual position scaled by a DIFFERENT ratio from
its 2560x1440 position (0.65-0.83 depending on the element) — Roblox GUI
objects mix Scale and Offset UDims per-element, so there's no one
formula. _COORDS_BY_WIDTH below holds directly-calibrated coordinate sets
per known screen width; config["screen"]["width"] picks the matching set
in __init__. A width with no calibrated set falls back to proportionally
scaling the 2560 set, which is a best-effort approximation, not exact —
recalibrate it properly the same way (real screenshots +
tools/capture_template.py / manual pixel-picking) if that ever matters.
Template matching (menu-open / victory-screen detection) uses
find_template_multiscale regardless of resolution — the templates
themselves were captured at 2560x1440, so on a smaller screen the real UI
element is smaller in absolute pixels than the template and a
single-scale match would miss it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from . import input_sim, vision

# Coordinates below were refined against a real recorded interaction
# (tools/record_macro.py, 2026-09-08) after discovering the root cause of
# every earlier failed test: those test scripts ran non-elevated while
# Roblox runs elevated (Windows UIPI blocks synthetic input — and, it
# turns out, input observation too — across that privilege boundary), not
# a coordinate or gesture problem. The original calibrated coordinates
# were actually already close; these are the recorded-click-confirmed
# values. This module runs inside the main macro process, which already
# self-elevates, so it doesn't hit that problem.
_REFERENCE_WIDTH = 2560

# Directly-calibrated coordinate sets, keyed by config["screen"]["width"].
# Each set: start_button, rookie_island, stage1_button, start_confirm_button,
# island_list_scroll_point, back_to_lobby_button.
_COORDS_BY_WIDTH: dict[int, dict[str, tuple[int, int]]] = {
    2560: {
        # Refined against a real recorded interaction
        # (tools/record_macro.py, 2026-09-08) after discovering the root
        # cause of every earlier failed test: those test scripts ran
        # non-elevated while Roblox runs elevated (Windows UIPI blocks
        # synthetic input — and, it turns out, input observation too —
        # across that privilege boundary), not a coordinate/gesture
        # problem. This module runs inside the main macro process, which
        # already self-elevates, so it doesn't hit that problem.
        "start_button": (1456, 998),
        "rookie_island": (920, 568),
        "stage1_button": (1237, 505),
        # Second "Start" click, on a follow-up team/loadout screen that
        # sometimes appears after the first Start — from the recorded
        # interaction, not separately verified against a live screenshot.
        # Live-tested at 1920x1080 (2026-09-08): this screen didn't appear
        # at all that time (Start went straight to "Teleporting..."), so
        # it may only show up in some circumstances (e.g. no team saved
        # yet). Clicking here when it's absent just clicks into the
        # in-game world, which is harmless.
        "start_confirm_button": (1993, 897),
        # Scroll wheel position — anywhere over the island list column works.
        "island_list_scroll_point": (920, 550),
        # The actual "campaign complete" signal: a standalone "VICTORY!"
        # results screen (not the Story menu at all — that was the wrong
        # theory before actually seeing it happen live, 2026-09-08) with a
        # "Back To Lobby" button.
        "back_to_lobby_button": (1672, 1032),
    },
    1920: {
        # Calibrated live against real 1920x1080 captures (2026-09-08):
        # rookie_island and start_button found precisely via
        # find_template_multiscale against story_rookie_island.png /
        # story_start_button.png; stage1_button eyeballed from a cropped
        # screenshot of the stage list (no template captured for it).
        "start_button": (1135, 824),
        "rookie_island": (602, 373),
        "stage1_button": (905, 340),
        # NOT live-verified — the confirm screen didn't appear during
        # calibration (see the 2560 entry's note), so there was nothing on
        # screen to calibrate against. Best-effort estimate only.
        "start_confirm_button": (1554, 744),
        "island_list_scroll_point": (602, 450),
        # NOT live-verified yet — proportionally estimated from the 2560
        # value (0.75x) pending a real Memory's End VICTORY screen capture
        # at this resolution. Recalibrate once that's been seen live.
        "back_to_lobby_button": (1254, 774),
    },
}


class StoryCampaign:
    def __init__(
        self,
        config: dict,
        capture: vision.ScreenCapture,
        running_event: threading.Event,
        logger: logging.Logger,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.cfg = config
        self.capture = capture
        self.running_event = running_event
        self.logger = logger
        self.log_callback = log_callback
        self._shutdown_flag = threading.Event()

        sc_cfg = config.get("story_campaign", {})
        self.check_interval_seconds = sc_cfg.get("check_interval_seconds", 120.0)
        self.forward_walk_seconds = sc_cfg.get("portal_walk_seconds", 3.0)
        self.forward_key = config["movement"]["keys"]["forward"]

        # See the module docstring's resolution-support note — use a
        # directly-calibrated coordinate set for this screen width if one
        # exists, otherwise fall back to proportionally scaling the 2560
        # reference set (best-effort approximation only).
        screen_width = config["screen"]["width"]
        coords = _COORDS_BY_WIDTH.get(screen_width)
        if coords is not None:
            self._start_button = coords["start_button"]
            self._rookie_island = coords["rookie_island"]
            self._stage1_button = coords["stage1_button"]
            self._start_confirm_button = coords["start_confirm_button"]
            self._island_list_scroll_point = coords["island_list_scroll_point"]
            self._back_to_lobby_button = coords["back_to_lobby_button"]
        else:
            self.logger.warning(
                f"No calibrated Campaign coordinates for screen width {screen_width} — "
                f"falling back to a proportional scale of the 2560-wide set (approximate)."
            )
            ref = _COORDS_BY_WIDTH[_REFERENCE_WIDTH]
            scale = screen_width / _REFERENCE_WIDTH
            self._start_button = (round(ref["start_button"][0] * scale), round(ref["start_button"][1] * scale))
            self._rookie_island = (round(ref["rookie_island"][0] * scale), round(ref["rookie_island"][1] * scale))
            self._stage1_button = (round(ref["stage1_button"][0] * scale), round(ref["stage1_button"][1] * scale))
            self._start_confirm_button = (
                round(ref["start_confirm_button"][0] * scale), round(ref["start_confirm_button"][1] * scale)
            )
            self._island_list_scroll_point = (
                round(ref["island_list_scroll_point"][0] * scale), round(ref["island_list_scroll_point"][1] * scale)
            )
            self._back_to_lobby_button = (
                round(ref["back_to_lobby_button"][0] * scale), round(ref["back_to_lobby_button"][1] * scale)
            )

        self.victory_template = vision.load_template("templates/story_victory_banner.png")
        if self.victory_template is None:
            self.logger.warning(
                "story_victory_banner.png template missing — campaign-finished detection won't work."
            )
        # The VICTORY! banner shows after EVERY stage clear, not just the
        # true final one (confirmed live: it fired after finishing just
        # Rookie Island Stage 1, resetting the whole run prematurely) — so
        # detection needs the "Memory's End" title alongside it too, since
        # that only shows on that specific stage's results screen.
        self.memorys_end_template = vision.load_template("templates/story_memorys_end_title.png")
        if self.memorys_end_template is None:
            self.logger.warning(
                "story_memorys_end_title.png template missing — campaign-finished detection won't work."
            )
        self.start_button_template = vision.load_template("templates/story_start_button.png")

        self.hide_overlays_fn: Optional[Callable[[], None]] = None
        self.show_overlays_fn: Optional[Callable[[], None]] = None

        self.stats = {"campaigns_completed": 0}

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

    def _is_at_final_stage(self, frame) -> bool:
        """Detects the true campaign-complete screen by the "Memory's End"
        title alone, not the "VICTORY!" banner. Originally required both
        (the banner alone fired after every stage clear, resetting the run
        early), but live testing on the real results screen showed the
        banner match scoring 0.73 against a 0.75 threshold — just below —
        almost certainly because of its animated shimmer/sparkle effect
        shifting pixels between captures. "Memory's End" only appears on
        that specific stage's results screen (confirmed by the user) and
        matched at 0.9999 live, so it's the more reliable signal on its
        own. Template match, not OCR, for the same reliability reasons
        door detection moved away from OCR-only signals."""
        if self.memorys_end_template is None:
            return False
        return vision.find_template_multiscale(frame, self.memorys_end_template, 0.75) is not None

    def _is_menu_open(self, frame) -> bool:
        if self.start_button_template is None:
            return True  # can't check — assume open rather than walking forever
        return vision.find_template_multiscale(frame, self.start_button_template, 0.75) is not None

    def _ensure_menu_open(self) -> None:
        """Walks forward into the lobby portal until the Story menu's Start
        button is actually visible — needed on a cold start (character
        just standing in the lobby, menu never opened) as well as after a
        reset, not just the reset case. Bounded attempts so a genuinely
        stuck character (facing the wrong way, stuck on scenery) doesn't
        walk forward forever."""
        frame = self._grab_frame()
        if self._is_menu_open(frame):
            return

        self._log("[CAMPAIGN] Story menu not open — walking forward into the portal.")
        for attempt in range(5):
            input_sim.hold_key_for(self.forward_key, self.forward_walk_seconds)
            time.sleep(0.5)
            frame = self._grab_frame()
            if self._is_menu_open(frame):
                return
            self._log(f"[CAMPAIGN] Menu still not open after walking (attempt {attempt + 1}/5) — trying again.")

        self._log("[RECOVERY] Story menu didn't open after repeated forward walks — proceeding anyway.")

    def _navigate_to_rookie_stage1_and_start(self) -> None:
        """Assumes the Story menu is already open. Scrolls the island list
        to the top (Rookie Island is always first), clicks it, clicks
        Stage 1, then Start."""
        self._log("[CAMPAIGN] Scrolling island list to the top.")
        # Confirmed via recording: real scrolling here is many individual
        # wheel ticks (dy=+1 each), not a handful of bigger ones — the
        # recorded interaction used ~25-30 ticks over a few seconds.
        for _ in range(30):
            input_sim.scroll_at(*self._island_list_scroll_point, clicks=1)
            time.sleep(0.08)

        self._log("[CAMPAIGN] Selecting Rookie Island.")
        # Was clicking this twice (matching a recorded interaction from
        # before click_at moved the cursor with real motion) — that was
        # only needed because a single instant-teleport click often didn't
        # register at all. Now that clicks move there properly, one click
        # is enough, and two was visibly registering as a double-click.
        input_sim.click_at(*self._rookie_island)
        time.sleep(0.3)

        self._log("[CAMPAIGN] Selecting Stage 1.")
        input_sim.click_at(*self._stage1_button)
        time.sleep(0.3)

        self._log("[CAMPAIGN] Clicking Start.")
        input_sim.click_at(*self._start_button)
        time.sleep(1.5)  # let the follow-up team/loadout screen load

        self._log("[CAMPAIGN] Clicking Start (confirm on team screen).")
        input_sim.click_at(*self._start_confirm_button)

    def _reset_and_restart(self) -> None:
        """Campaign hit its true end (the "VICTORY!" results screen).
        Clicks "Back To Lobby", walks forward into the portal to reopen
        the Story menu, and starts the whole campaign over from Rookie
        Island Stage 1."""
        self.stats["campaigns_completed"] += 1
        self._log(
            f"[CAMPAIGN] Reached the end (VICTORY screen) — "
            f"campaign #{self.stats['campaigns_completed']} complete. Restarting from Rookie Island."
        )
        input_sim.click_at(*self._back_to_lobby_button)
        time.sleep(1.0)  # scene transition back to the lobby takes a moment

        self._ensure_menu_open()
        self._navigate_to_rookie_stage1_and_start()

    def run(self) -> None:
        self._log("[INFO] Story campaign automation enabled")

        try:
            while not self._shutdown_flag.is_set():
                if not self.running_event.is_set():
                    time.sleep(0.1)
                    continue

                try:
                    # If the campaign toggle turns on while already sitting on
                    # the final VICTORY/Memory's End results screen (e.g. the
                    # run just finished and the toggle is pressed manually, or
                    # a restart happened mid-flow), handle that FIRST — trying
                    # to open the Story menu from here would just walk into
                    # nothing since the results screen isn't the lobby.
                    frame = self._grab_frame()
                    if self._is_at_final_stage(frame):
                        self._reset_and_restart()
                    else:
                        # Cold start (character just standing in the lobby,
                        # menu never opened) needs the same forward-walk as a
                        # post-reset restart — check first rather than
                        # assuming the menu is already open.
                        self._ensure_menu_open()
                        self._navigate_to_rookie_stage1_and_start()

                    # Now just watch for the "reached the very end" signal.
                    # Not fully validated yet what the screen looks like
                    # during the many-hour auto-progression stretch (does
                    # the menu stay open? does it need reopening each
                    # check?) — this polls by simply re-checking the
                    # current frame at an interval; if that turns out not
                    # to reflect live progress, the check itself will need
                    # to actively reopen the menu each time, same
                    # iterative-fix pattern as everything else tonight.
                    while not self._shutdown_flag.is_set() and self.running_event.is_set():
                        frame = self._grab_frame()
                        if self._is_at_final_stage(frame):
                            self._reset_and_restart()
                            break
                        # Sleep in 1s increments, not one big
                        # check_interval_seconds sleep — pressing B to stop
                        # mid-wait was taking up to 2 minutes to actually
                        # register (looked completely unresponsive) since
                        # the loop only re-checks running_event once the
                        # sleep call returns.
                        waited = 0.0
                        while waited < self.check_interval_seconds:
                            if self._shutdown_flag.is_set() or not self.running_event.is_set():
                                break
                            time.sleep(1.0)
                            waited += 1.0

                except Exception as e:
                    self.logger.error("Story campaign cycle failed: %s", e)
                    self._log(f"[RECOVERY] Campaign cycle failed, retrying: {e}")
                    time.sleep(5.0)

        except Exception as e:
            self.logger.error("Story campaign automation crashed: %s", e)
            self._log(f"[ERROR] {e}")
        finally:
            input_sim.release_all(self.forward_key)

    def stop(self) -> None:
        self._shutdown_flag.set()
        input_sim.release_all(self.forward_key)
