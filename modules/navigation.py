"""
navigation.py — detects every door type currently on screen, picks the
best-ranked one (per door_detection.priority in config), and walks to it.

This is the validated logic from earlier testing:
- Multi-scale template matching (doors are 3D-world objects whose on-screen
  size changes with camera distance — a single fixed-size match misses
  constantly).
- Priority ranking with per-type threshold overrides (a generic-shaped icon
  can false-match more than a distinctive one).
- Click-to-move (2026-09-09, movement.click_to_move) is now the primary way
  to approach a visible door: RIGHT-click (click_to_move_button) its
  approximate ground point and let Roblox walk there. User-confirmed
  2026-09-09: the character still has to be roughly facing the direction
  it's meant to walk, so _ensure_first_person locks the camera to
  first-person once (screen-center then reliably matches actual facing,
  unlike a free third-person camera). Falls back to a WASD
  camera-align-then-hold-forward approach (movement.click_to_move: false)
  if click-to-move isn't enabled in-game.
- Camera turning (arrow keys or mouse-drag) is used only by scan_tick, a
  bounded, stationary multi-direction sweep to look around for a door
  that isn't in view at all — it never walks forward while turning, to
  avoid blindly running into obstacles while searching.
- A focus-guarantee click before every movement action, since Windows
  blocks external processes from forcing foreground focus on demand.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

import cv2

from . import ai_vision, input_sim, self_tuning, vision


class DoorNavigator:
    def __init__(self, config: dict, capture: vision.ScreenCapture, logger: logging.Logger):
        self.cfg = config
        self.capture = capture
        self.logger = logger

        mv = config["movement"]
        self.forward_key = mv["keys"]["forward"]
        self.back_key = mv["keys"]["back"]
        self.left_key = mv["keys"]["left"]
        self.right_key = mv["keys"]["right"]
        turn_keys = mv.get("camera_turn_keys", {"left": "left", "right": "right"})
        self.camera_left_key = turn_keys["left"]
        self.camera_right_key = turn_keys["right"]
        # "mouse" (default) drags via right-click-hold + cursor movement —
        # Roblox's native camera control, turns more reliably than the
        # arrow-key binding. "keys" falls back to camera_turn_keys above.
        self.camera_turn_method = mv.get("camera_turn_method", "mouse")
        self.camera_drag_pixels = mv.get("camera_drag_pixels", 400)
        # Click-to-move (2026-09-09) — see config.yaml's movement.click_to_move
        # comment. Skips camera alignment entirely: click the door's ground
        # point, let Roblox's own pathfinding walk there.
        self.click_to_move = mv.get("click_to_move", True)
        self.click_to_move_ground_offset_fraction = mv.get("click_to_move_ground_offset_fraction", 0.35)
        # Live-tested 2026-09-09: a door badge icon sits high in the frame
        # (~25% down) and is small on screen, so match.h * offset_fraction
        # alone barely moves the click off the icon — it was landing on the
        # archway/wall, not the floor, so click-to-move never registered any
        # movement at all ("its not going to the doors"). This floor makes
        # sure the click always lands at LEAST this far down the frame
        # (fraction of frame height) regardless of how small the icon is,
        # while a closer/bigger icon can still push it lower via the
        # offset_fraction above if that ends up being further down anyway.
        self.click_to_move_ground_min_fraction = mv.get("click_to_move_ground_min_fraction", 0.55)
        self.click_to_move_arrival_wait_seconds = mv.get("click_to_move_arrival_wait_seconds", 2.0)
        # User-confirmed 2026-09-09: this game's click-to-move is RIGHT-click
        # (not left), and the character has to already be roughly facing a
        # direction to walk that way — first-person keeps screen-center
        # aligned with facing direction, unlike a free third-person camera.
        self.click_to_move_button = mv.get("click_to_move_button", "right")
        # User-confirmed 2026-09-09: right-click-to-move accepts a click
        # directly on the door itself, no ground-offset guess needed. Set to
        # false to go back to the computed-ground-point approach below.
        self.click_to_move_click_on_door = mv.get("click_to_move_click_on_door", True)
        self.click_to_move_first_person = mv.get("click_to_move_first_person", True)
        self.click_to_move_zoom_in_clicks = mv.get("click_to_move_zoom_in_clicks", 15)
        self._first_person_set = False
        fwd_pt = mv.get("click_to_move_forward_click_point", [0.5, 0.55])
        self._forward_click_fx, self._forward_click_fy = fwd_pt[0], fwd_pt[1]
        self.interact_key = mv.get("interact_key")
        self.turn_seconds = mv.get("turn_seconds", 0.4)
        # approach_and_enter's alignment loop: how long each camera-turn
        # correction tap holds, and the max number of turn-then-recheck
        # cycles before giving up and walking anyway (so a door that's hard
        # to keep centered — e.g. detection flickering — can't stall the
        # approach forever).
        self.align_turn_seconds = mv.get("align_turn_seconds", 0.05)
        self.align_max_attempts = mv.get("align_max_attempts", 6)
        self.walk_through_seconds = mv.get("walk_through_seconds", 3.0)
        # After clearing the doorway, keep walking in these increments,
        # checking the "Floor: N" HUD text each time, until it actually
        # increments (confirms the room transition really happened, not
        # just "walked forward for a fixed guess and hoped") — then a bit
        # further to reach the new platform's center (where the floor
        # number is painted on the ground), capped so a missed OCR read
        # can't walk forever/off the map.
        self.walk_to_center_step_seconds = mv.get("walk_to_center_step_seconds", 0.5)
        self.walk_to_center_max_seconds = mv.get("walk_to_center_max_seconds", 8.0)
        self.walk_to_center_extra_seconds = mv.get("walk_to_center_extra_seconds", 1.5)
        self.scan_sweep_directions = mv.get("scan_sweep_directions", 4)
        self.scan_sweep_turn_seconds = mv.get("scan_sweep_turn_seconds", 0.5)
        self.scan_sweep_pause_seconds = mv.get("scan_sweep_pause_seconds", 0.3)

        # Jumping only happens once, right after walking through a door.
        jump_cfg = config.get("auto_jump", {})
        self.jump_key = mv.get("jump_key", "space") if jump_cfg.get("enabled", True) else None
        self.jump_hold_seconds = jump_cfg.get("hold_seconds", 0.05)

        dd = config["door_detection"]
        self.method = dd.get("method", "template")

        # "yolo" — a trained object detector instead of template matching.
        # Learns actual visual features rather than doing raw pixel
        # correlation, which is what made Mystery/Combat/Lunarium keep
        # cross-confusing each other under template matching regardless of
        # threshold tuning. See tools/label_doors.py + train_door_detector.py
        # to build one; class names still come from door_detection.templates
        # below so priority/threshold overrides work the same either way.
        self.yolo_model = None
        self.yolo_confidence = dd.get("yolo_confidence", 0.5)
        if self.method == "yolo":
            model_path = dd.get("yolo_model_path", "models/door_detector.pt")
            if os.path.isfile(model_path):
                from ultralytics import YOLO
                self.yolo_model = YOLO(model_path)
            else:
                self.logger.warning(
                    "door_detection.method is 'yolo' but no model found at %s — "
                    "train one with tools/train_door_detector.py, falling back to template matching.",
                    model_path,
                )
                self.method = "template"

        # Same order train_door_detector.py used to build data.yaml, so a
        # trained model's class indices map back to these names correctly.
        self.yolo_class_names = [entry["name"] for entry in dd.get("templates", [])]

        self.named_templates: list[tuple[str, "vision.np.ndarray"]] = []
        for entry in dd.get("templates", []):
            img = vision.load_template(entry["path"])
            if img is not None:
                self.named_templates.append((entry["name"], img))
            else:
                self.logger.warning("Door template not found: %s", entry["path"])

        # detect_door() was measured at ~8.3s/call at full 2560x1440 — 8
        # templates x 7 scales of matchTemplate on a huge frame. Matching
        # against a downscaled frame with proportionally downscaled
        # templates finds the exact same matches (the multi-scale search is
        # relative, so shrinking both together doesn't change what's
        # findable) on far fewer pixels. Precomputed once here, not per call.
        self.detection_scale = dd.get("detection_scale", 0.45)
        self._scaled_templates: list[tuple[str, "vision.np.ndarray"]] = [
            (name, cv2.resize(img, None, fx=self.detection_scale, fy=self.detection_scale,
                               interpolation=cv2.INTER_AREA))
            for name, img in self.named_templates
        ]

        # Lower rank number = higher priority. Anything not listed sorts last
        # (but is still a valid fallback if it's the only door visible).
        priority_list = dd.get("priority", [])
        self.priority_rank = {name: i for i, name in enumerate(priority_list)}

        # "ai_hybrid" — an AI vision call decides WHICH type is present
        # (semantic understanding, immune to the per-floor biome reskins and
        # stray-texture false-positives that broke plain template matching
        # tonight), then ordinary local template matching for just that one
        # confirmed type finds its precise pixel location — no other type is
        # competing, so the cross-type confusion problem (Combat vs
        # Merchant, Mystery vs Combat) can't happen at all. See ai_vision.py
        # for why raw AI-reported coordinates aren't used directly (tested:
        # 250-440px off on a 2560px frame, not precise enough to strafe by).
        all_type_names = [n for n, _ in self.named_templates]
        self._ai_priority_order = sorted(all_type_names, key=self._rank_of) if all_type_names else []
        self.ai_classifier = ai_vision.AIDoorClassifier(config, self.logger) if self.method == "ai_hybrid" else None
        if self.method == "ai_hybrid" and self.ai_classifier is not None and not self.ai_classifier.enabled:
            self.logger.warning(
                "door_detection.method is 'ai_hybrid' but the AI classifier couldn't initialize "
                "(see warning above) — falling back to template matching."
            )
            self.method = "template"

        self.match_threshold = dd.get("match_threshold", 0.75)
        # Per-type overrides: some icons are generic enough in shape to
        # coincidentally correlate with other circular UI elements, giving
        # them a higher false-positive rate than a distinctive icon.
        self.threshold_overrides: dict[str, float] = dd.get("thresholds", {})
        self.color_cfg = dd.get("color_fallback", {})

        # The badge icon templates (recaptured 2026-09-07 as tight crops to
        # survive per-floor biome reskins) aren't all centered within the
        # door archway they were cropped from — e.g. mystery's "?" sits
        # ~17% of the door's width left of center, combat's ~4.5% right.
        # Aligning by the badge's on-screen center alone was walking into
        # the SIDE of the door, not the middle. Value = (badge center -
        # true door center) / door width, from the original capture crop;
        # subtracted (scaled by the badge's current on-screen width) when
        # computing how far off-center the door itself actually is.
        self.badge_center_offset: dict[str, float] = dd.get("badge_center_offset", {})

        # Doors never actually appear in the top HUD strip or the bottom
        # hero-portrait/teleport-button row in any screenshot we've seen —
        # false positives keep landing there (hero portraits, AUTO timers,
        # currency icons). Restricting the search region to exclude those
        # bands eliminates that whole category at once, rather than chasing
        # each new false-positive spot individually. [x, y, w, h] or None
        # for the full frame.
        self.search_region: list[int] | None = dd.get("search_region")

        region = config["screen"]["game_region"]
        self.frame_w = region[2]
        self.frame_h = region[3]

        # Same "Floor: N" HUD region the GUI mirrors into its overlay —
        # reused here to confirm a room transition actually happened after
        # walking through a door (see _floor_region_crop).
        self.floor_region: list[int] | None = config.get("overlay", {}).get("floor_region")

        self.last_match_name: str | None = None
        # Debug/tracer info for the GUI's overlay — every candidate seen on
        # the most recent detect_door() call, and which one (if any) was
        # chosen, so "why did it go the wrong way" is visible on screen
        # instead of having to guess from log lines.
        self.last_all_candidates: list[tuple[str, vision.Match]] = []
        self.last_chosen_match: vision.Match | None = None
        self.deadzone_fraction = 0.06  # kept in sync with approach_and_enter's own deadzone

        # "Floor Cleared" banner — a door can be visible in the background
        # before the encounter is actually finished, so Towers waits for
        # this to confirm before walking. Flat 2D HUD text, no multi-scale
        # search needed (unlike the doors themselves).
        fc_cfg = config.get("floor_cleared", {})
        # "ocr" reads the actual banner text via Tesseract instead of image
        # correlation — no threshold to tune, and immune to the banner's
        # fade/scale-in animation (which was why the template method only
        # confirmed ~1 in 4 real clears even after lowering its threshold).
        self.floor_cleared_method = fc_cfg.get("method", "ocr")
        self.floor_cleared_ocr_region = fc_cfg.get("ocr_region")  # [x,y,w,h] or None -> search_region
        self.floor_cleared_keyword = fc_cfg.get("ocr_keyword", "CLEARED")

        self._ocr_warned = False

        fc_path = fc_cfg.get("template_path", "")
        self.floor_cleared_template = vision.load_template(fc_path) if fc_path else None
        self.floor_cleared_threshold = fc_cfg.get("match_threshold", 0.75)
        if self.floor_cleared_method == "template" and fc_path and self.floor_cleared_template is None:
            self.logger.warning("Floor-cleared template not found: %s", fc_path)

        if self.method == "template" and not self.named_templates:
            self.logger.warning(
                "door_detection.method is 'template' but no valid templates loaded — "
                "capture some with tools/capture_template.py, or switch to 'color'."
            )

        # Wired up by main.py after the GUI is constructed, same as
        # towers.py's own hide/show hooks — the tracer overlay is a
        # FULL-SCREEN window, so any of this module's own capture.grab()
        # calls (the alignment loop, the floor-number confirmation check)
        # need the same hide-before/show-after protection towers.py's
        # detection captures already had, or tracer lines/boxes can get
        # baked directly into the frames used to judge alignment.
        self.hide_overlays_fn: Optional[Callable[[], None]] = None
        self.show_overlays_fn: Optional[Callable[[], None]] = None

        # Tracks whether alignment/walk actions actually worked, and
        # auto-adjusts (then persists to config.yaml) a bounded setting
        # when the same kind of failure repeats — see self_tuning.py.
        self.self_tuner = self_tuning.SelfTuner(config, "config.yaml", self.logger)

    def _grab_frame(self):
        """capture.grab() with the macro's own overlay windows hidden for
        the instant of the screenshot — see hide_overlays_fn above."""
        if self.hide_overlays_fn:
            self.hide_overlays_fn()
        try:
            return self.capture.grab()
        finally:
            if self.show_overlays_fn:
                self.show_overlays_fn()

    def _rank_of(self, name: str) -> int:
        return self.priority_rank.get(name, len(self.priority_rank))

    # Local matching here trusts that the type is already confirmed to be
    # present (by the AI, or by the caller re-checking a type it already
    # found) — but with threshold 0.0, find_template_multiscale always
    # returns SOME location, even a near-zero-correlation match against
    # pure noise if the icon genuinely isn't findable in this particular
    # frame (motion blur, mid-transition, occluded). A low sanity floor
    # rejects that garbage without demanding the high confidence plain
    # template matching needed (there's no cross-type collision risk here
    # to threshold-tune around, unlike detect_door()'s multi-candidate case).
    _LOCATE_MIN_SANE_SCORE = 0.35

    # How far (in screen pixels) the same physical door can plausibly have
    # moved between one re-check and the next during alignment — wide
    # enough to comfortably cover a real camera-turn tick's movement, tight
    # enough to exclude a second, visually-identical door sitting next to
    # it (two "Combat" gates side by side were observed roughly 700-800px
    # apart on screen at typical approach distance).
    _NEAR_X_WINDOW = 350

    def locate_type(self, frame, type_name: str, near_x: float | None = None) -> vision.Match | None:
        """Local (no API call) template match for one specific, already-
        known door type — used both by ai_hybrid detection (nothing else is
        in contention once the AI has said what's there) and by
        approach_and_enter's alignment loop (cheap enough to call every
        camera-turn tick, unlike re-asking the AI each time).

        near_x, when given, restricts the search to a window around that
        x-coordinate — critical whenever two doors of the SAME type are
        visible side by side (e.g. two "Combat" gates): without it, the
        plain global-best-match search can silently flip between the two
        near-identical doors from one call to the next as their scores
        jitter, making the alignment loop see the target "teleport"
        sideways and oscillate against a moving goalpost instead of a
        single real target — this was observed live, walking into the gap
        between two such doors instead of either one."""
        template = next((img for name, img in self.named_templates if name == type_name), None)
        if template is None:
            return None

        if self.search_region:
            sx, sy, sw, sh = self.search_region
            search_frame = frame[sy:sy + sh, sx:sx + sw]
        else:
            sx, sy = 0, 0
            search_frame = frame

        if near_x is not None:
            m = vision.find_template_multiscale_near(
                search_frame, template, self._LOCATE_MIN_SANE_SCORE, near_x - sx, self._NEAR_X_WINDOW,
            )
        else:
            m = vision.find_template_multiscale(search_frame, template, self._LOCATE_MIN_SANE_SCORE)
        if m is None:
            return None
        return vision.Match(x=m.x + sx, y=m.y + sy, w=m.w, h=m.h, score=m.score)

    def _centering_offset(self, match: vision.Match, door_type: str | None) -> float:
        """How far off-center the DOOR (not just the badge icon) actually
        is, in pixels — the badge's own on-screen center corrected by its
        known bias within the door archway (see badge_center_offset),
        scaled to the badge's current apparent size so it stays correct at
        any camera distance."""
        cx, _ = match.center
        raw_offset = cx - self.frame_w / 2
        bias_fraction = self.badge_center_offset.get(door_type, 0.0) if door_type else 0.0
        return raw_offset - bias_fraction * match.w

    def _floor_region_crop(self, frame):
        """Crop of the "Floor: N" HUD text — used to confirm a room
        transition actually happened after walking through a door, rather
        than just walking forward for a fixed guess and hoping.

        Used to be OCR-read into an actual int (see git history), but live
        testing (2026-09-08) found Tesseract simply cannot read this game's
        bold outlined/bubble font AT ALL — every preprocessing variant
        tried (upscaling, thresholding in both directions, digit-only
        whitelist, every --psm mode) read "Floor: 24" as "BR", "NFloonzecmay",
        or nothing. That silent, permanent failure was feeding
        self_tuning.py a constant stream of "confirmation failed", which
        kept inflating movement.walk_through_seconds toward its cap every
        few doors for no real reason — chasing a signal that could never
        succeed.

        This region is a fixed 2D HUD element (not a 3D-world object), so
        pixel-for-pixel it's provably identical frame-to-frame whenever the
        number hasn't changed (confirmed live: 0.0 mean diff across a full
        second on the same floor) — a plain pixel comparison (see
        vision.mean_pixel_diff) confirms a real transition just as
        reliably as reading the actual digits would, without needing OCR
        to work on this font at all."""
        if not self.floor_region:
            return None
        return vision.crop(frame, self.floor_region)

    def detect_door(self, frame) -> vision.Match | None:
        """Also records the chosen door's name in self.last_match_name (None
        if nothing was found), plus every candidate seen this call in
        last_all_candidates and the chosen one in last_chosen_match — for
        the GUI's tracer overlay."""
        self.last_match_name = None
        self.last_all_candidates = []
        self.last_chosen_match = None

        if self.method == "ai_hybrid" and self.ai_classifier is not None:
            visible_types = self.ai_classifier.list_visible_types(frame, self._ai_priority_order)
            if not visible_types:
                return None
            # AI reports perception (what's visible); priority ranking is
            # our own established logic, not something to trust the model
            # to re-derive — same _rank_of used for template candidates.
            chosen_type = min(visible_types, key=self._rank_of)

            # Local-match ONLY the confirmed type — nothing else is in
            # contention, so there's no cross-type priority collision to
            # threshold-tune around like plain template matching had.
            located = self.locate_type(frame, chosen_type)
            if located is None:
                self.logger.warning(
                    "AI said '%s' is visible but local matching couldn't find it at all — skipping this cycle.",
                    chosen_type,
                )
                return None

            self.last_all_candidates = [(chosen_type, located)]
            self.last_match_name = chosen_type
            self.last_chosen_match = located
            return located

        if self.method == "yolo" and self.yolo_model is not None:
            if self.search_region:
                sx, sy, sw, sh = self.search_region
                search_frame = frame[sy:sy + sh, sx:sx + sw]
            else:
                sx, sy = 0, 0
                search_frame = frame

            results = self.yolo_model.predict(search_frame, conf=self.yolo_confidence, verbose=False)[0]
            candidates: list[tuple[str, vision.Match]] = []
            for box in results.boxes:
                cls_id = int(box.cls[0])
                if cls_id >= len(self.yolo_class_names):
                    continue
                name = self.yolo_class_names[cls_id]
                score = float(box.conf[0])
                if score < self.threshold_overrides.get(name, self.yolo_confidence):
                    continue
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                candidates.append((name, vision.Match(
                    x=x1 + sx, y=y1 + sy, w=x2 - x1, h=y2 - y1, score=score,
                )))

            self.last_all_candidates = candidates
            if candidates:
                name, match = min(candidates, key=lambda nm: self._rank_of(nm[0]))
                if len(candidates) > 1:
                    seen = ", ".join(f"{n}({m.score:.2f})" for n, m in candidates)
                    self.logger.info("Doors visible: %s — chose '%s'.", seen, name)
                self.last_match_name = name
                self.last_chosen_match = match
                return match
            return None

        if self.method == "template" and self.named_templates:
            if self.search_region:
                sx, sy, sw, sh = self.search_region
                search_frame = frame[sy:sy + sh, sx:sx + sw]
            else:
                sx, sy = 0, 0
                search_frame = frame

            scale = self.detection_scale
            small_frame = cv2.resize(search_frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

            # Score everything first (threshold 0.0), then apply each type's
            # own effective threshold. Matched against the downscaled,
            # region-cropped frame + downscaled templates for speed;
            # rescale/offset coordinates back to real screen space right after.
            raw_small = vision.find_all_named_templates_multiscale(small_frame, self._scaled_templates, 0.0)
            raw = [
                (name, vision.Match(
                    x=int(m.x / scale) + sx, y=int(m.y / scale) + sy,
                    w=int(m.w / scale), h=int(m.h / scale), score=m.score,
                ))
                for name, m in raw_small
            ]
            candidates = [
                (name, m) for name, m in raw
                if m.score >= self.threshold_overrides.get(name, self.match_threshold)
            ]
            self.last_all_candidates = candidates
            if candidates:
                name, match = min(candidates, key=lambda nm: self._rank_of(nm[0]))
                if len(candidates) > 1:
                    seen = ", ".join(f"{n}({m.score:.2f})" for n, m in candidates)
                    self.logger.info("Doors visible: %s — chose '%s'.", seen, name)
                self.last_match_name = name
                self.last_chosen_match = match
                return match

        if self.color_cfg.get("enabled", False):
            match = vision.find_color_blob(
                frame,
                self.color_cfg["hsv_lower"],
                self.color_cfg["hsv_upper"],
                self.color_cfg.get("min_contour_area", 2500),
            )
            if match:
                self.last_match_name = "color"
                self.last_chosen_match = match
                self.last_all_candidates = [("color", match)]
            return match
        return None

    def floor_cleared_score(self, frame) -> float:
        """Raw best-match score for the "Floor Cleared" banner (0.0 if no
        template configured) — exposed separately from is_floor_cleared so
        a timeout can log how close it actually got, instead of a silent
        miss. Template method only."""
        if self.floor_cleared_template is None:
            return 0.0
        m = vision.find_template(frame, self.floor_cleared_template, 0.0)
        return m.score if m else 0.0

    def floor_cleared_status(self, frame) -> tuple[bool, str]:
        """Returns (is_cleared, diagnostic) — diagnostic is the raw OCR text
        seen (ocr method) or the match score (template method), so a timeout
        can log exactly what it saw instead of a silent miss."""
        if self.floor_cleared_method == "ocr":
            try:
                region = self.floor_cleared_ocr_region or self.search_region
                text = vision.ocr_text(frame, region=region).strip()
                cleared = self.floor_cleared_keyword.upper() in text.upper()
                return cleared, (text.replace("\n", " ") or "<no text read>")
            except Exception as e:
                # Most likely Tesseract-OCR isn't installed on this machine
                # (a separate system install, not something a shared .exe
                # can bundle). Falls back to "not cleared" rather than
                # crashing the whole Towers thread — the wait_timeout_seconds
                # fallback still lets it proceed, just without early
                # confirmation, same as if no template were configured.
                if not self._ocr_warned:
                    self.logger.warning(
                        "floor_cleared OCR failed (%s) — is Tesseract-OCR installed? "
                        "Falling back to always waiting out the timeout.", e,
                    )
                    self._ocr_warned = True
                return False, "<OCR unavailable>"
        score = self.floor_cleared_score(frame)
        return score >= self.floor_cleared_threshold, f"score {score:.2f}"

    def is_floor_cleared(self, frame) -> bool:
        """True if the "Floor Cleared" banner is currently on screen. If
        using the template method with no template configured, always
        returns True (gate skipped, not blocking) rather than stalling
        Towers indefinitely."""
        if self.floor_cleared_method == "template" and self.floor_cleared_template is None:
            return True
        cleared, _ = self.floor_cleared_status(frame)
        return cleared

    def save_debug_image(self, frame, path: str) -> None:
        """Draws every candidate from the most recent detect_door() call
        onto a copy of the frame that was actually detected on (green =
        chosen, gray = seen but not picked, with name+score labels) and
        writes it to disk — a static, self-contained record of exactly what
        was detected, for reviewing later without needing a live screen."""
        img = frame.copy()
        for name, m in self.last_all_candidates:
            is_chosen = (m is self.last_chosen_match)
            color = (0, 255, 0) if is_chosen else (150, 150, 150)  # BGR
            cv2.rectangle(img, (m.x, m.y), (m.x + m.w, m.y + m.h), color, 3 if is_chosen else 2)
            label = f"{name} {m.score:.2f}" + (" CHOSEN" if is_chosen else "")
            cv2.putText(img, label, (m.x, max(0, m.y - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        cv2.imwrite(path, img)

    def _ensure_game_focus(self) -> None:
        """Windows won't let an external process steal foreground focus on
        demand — so if literally anything else took focus since the last
        action, keyboard presses would silently go nowhere while mouse
        clicks would still work (a click activates whatever window it lands
        on). Click once on a neutral spot of the game world — never a HUD
        button — right before sending any movement keys."""
        region = self.cfg["screen"]["game_region"]
        x = region[0] + self.frame_w // 2
        y = region[1] + int(self.frame_h * 0.4)  # open world space, well above the HUD/hero row
        input_sim.click_at(x, y)

    def _align_with_ai(self, door_type: str) -> bool:
        """Alignment steered by the AI: each attempt, ask it whether the
        named door is left/right/centered on screen and turn accordingly —
        a categorical judgment plays to what a vision model is actually
        reliable at, unlike asking it for exact pixel coordinates (tested:
        250-440px off on a 2560px frame). One AI call per attempt (not just
        once for the original detection), so this costs more than the
        local-matching loop — that trade is intentional here, per explicit
        instruction to have the AI drive as much of this as it reliably
        can. Returns True if it actually reached "centered", False if it
        ran out of attempts or lost sight of the door — self_tuner uses
        this to notice a repeated failure pattern and adjust."""
        step_seconds = self.align_turn_seconds
        prev_direction = None
        near_x = self.last_chosen_match.center[0] if self.last_chosen_match else None
        for attempt in range(self.align_max_attempts):
            frame = self._grab_frame()

            # AI alignment's actual turn decision comes from judge_alignment
            # below, not from this — this is purely so the GUI's tracer
            # overlay tracks the real door position as the camera turns.
            # Without it, last_chosen_match stays frozen at wherever
            # detect_door() saw the door BEFORE alignment started, so the
            # drawn tracer box stays fixed in screen-space while the camera
            # rotates the world underneath it — looks exactly like "the box
            # just moves with the camera instead of tracking the symbol"
            # (reported live, 2026-09-08), which is real: it doesn't track
            # anything during AI alignment, it's just stale. Cheap local
            # template match, no API cost, same one _align_locally already
            # uses every attempt.
            relocated = self.locate_type(frame, door_type, near_x=near_x)
            if relocated is not None:
                self.last_chosen_match = relocated
                self.last_all_candidates = [(door_type, relocated)]
                near_x = relocated.center[0]

            judgment = self.ai_classifier.judge_alignment(frame, door_type)
            self.logger.info(
                "AI alignment judgment for '%s': %s (attempt %d/%d, step %.3fs).",
                door_type, judgment, attempt + 1, self.align_max_attempts, step_seconds,
            )
            if judgment == "centered":
                return True
            if judgment == "lost":
                self.logger.info("AI lost sight of '%s' while aligning — proceeding with best-known heading.", door_type)
                return False

            # Same overshoot fix as the local method: if the AI flips
            # direction from what it just said (told to turn right, now
            # says turn left), the tap is turning further than needed —
            # halve it instead of repeating the same too-large nudge.
            if prev_direction is not None and judgment != prev_direction:
                step_seconds = max(step_seconds / 2.0, 0.02)
                self.logger.info("Overshot center — halving turn step to %.3fs.", step_seconds)
            prev_direction = judgment

            turn_key = self.camera_right_key if judgment == "right" else self.camera_left_key
            input_sim.tap_key(turn_key, step_seconds)

        self.logger.info("AI alignment attempts exhausted for '%s' — proceeding anyway.", door_type)
        return False

    def _align_locally(self, door_type: str, match: vision.Match) -> bool:
        """Local (no API call), pixel-offset-based alignment — the fallback
        when AI steering isn't available (door_detection.method isn't
        ai_hybrid, or the AI classifier failed to initialize). Loops
        turn-a-bit -> re-check -> turn-a-bit again until the door is
        actually centered (or attempts run out), unlike a single
        fixed-duration strafe guess committed to blindly (the old approach
        — no real feedback loop, so a bad initial read could still walk
        confidently off the map). Returns True if it actually reached
        centered, False otherwise — self_tuner uses this to notice a
        repeated failure pattern and adjust."""
        current = match
        deadzone = self.frame_w * 0.06
        # Adaptive (bisection-style) step: if a turn overshoots past center
        # (the offset flips sign from what it was), the fixed-duration tap
        # is too big for how close we already are — halve it before trying
        # again. Without this, a fixed tap size that happens to be a bit
        # too large just oscillates left/right/left/right forever without
        # ever narrowing in (this is exactly what was observed live: every
        # single attempt flipped direction, using all 6 tries with no
        # convergence).
        step_seconds = self.align_turn_seconds
        prev_offset = None

        for attempt in range(self.align_max_attempts):
            # Keep the GUI's tracer overlay showing the real, current
            # position too, not just whatever detect_door() saw before
            # alignment started.
            self.last_chosen_match = current
            self.last_all_candidates = [(door_type, current)]

            offset = self._centering_offset(current, door_type)
            if abs(offset) <= deadzone:
                return True

            if prev_offset is not None and (offset > 0) != (prev_offset > 0):
                # Confirmed live: even a 0.1s starting tap turns the camera
                # well past the door — the turns clearly DO register at
                # small durations, the base was just too large, not too
                # small. Floor lowered to 0.02 to allow tighter convergence
                # now that the starting point itself has been corrected
                # (see align_turn_seconds).
                step_seconds = max(step_seconds / 2.0, 0.02)
                self.logger.info("Overshot center — halving turn step to %.3fs.", step_seconds)
            prev_offset = offset

            turn_key = self.camera_right_key if offset > 0 else self.camera_left_key
            self.logger.info(
                "Turning camera %s toward '%s' (attempt %d/%d, step %.3fs).",
                "right" if offset > 0 else "left", door_type, attempt + 1, self.align_max_attempts, step_seconds,
            )
            input_sim.tap_key(turn_key, step_seconds)

            frame = self._grab_frame()
            near_x, _ = current.center
            relocated = self.locate_type(frame, door_type, near_x=near_x)
            if relocated is None:
                self.logger.info("Lost sight of '%s' while aligning — proceeding with best-known heading.", door_type)
                return False
            current = relocated

        self.logger.info("Alignment attempts exhausted for '%s' — proceeding anyway.", door_type)
        return False

    def _click_walk_forward(self) -> None:
        """Clicks the fixed near-bottom-center screen point (see
        click_to_move_forward_click_point) to nudge further forward via
        click-to-move — almost always walkable ground directly in front of
        wherever the character currently is, regardless of which door was
        just walked to."""
        fx = int(self.frame_w * self._forward_click_fx)
        fy = int(self.frame_h * self._forward_click_fy)
        input_sim.click_at(fx, fy, button=self.click_to_move_button)

    def _ensure_first_person(self) -> None:
        """User-confirmed 2026-09-09: click-to-move here needs the character
        already roughly facing the direction it's meant to walk — a free
        third-person camera can point anywhere regardless of facing, but
        first-person keeps screen-center locked to the actual facing
        direction, which is what click-to-move's on-screen target position
        needs to line up with. Zooms the camera all the way in (Roblox's
        default scroll-to-zoom) once per navigator lifetime — first-person,
        once reached, persists on its own after that."""
        if not self.click_to_move_first_person or self._first_person_set:
            return
        self._ensure_game_focus()
        cx = self.frame_w // 2
        cy = int(self.frame_h * 0.4)
        input_sim.scroll_at(cx, cy, self.click_to_move_zoom_in_clicks)
        self.logger.info("Zoomed camera to first-person for click-to-move.")
        self._first_person_set = True

    def _approach_and_enter_clickmove(self, match: vision.Match, door_type: str | None) -> None:
        """Click-to-move variant of approach_and_enter (2026-09-09). Two
        corrections from live user feedback the same day: (1) the move
        button is RIGHT-click here, not left (click_to_move_button); (2)
        the character has to already be roughly facing a direction to walk
        that way, unlike a pure NavMesh-pathfind-anywhere click-to-move —
        _ensure_first_person locks the camera to first-person once so
        screen-center reliably matches actual facing direction, which a
        free third-person camera can't guarantee. Clicks the door's
        approximate ground point once (see click_to_move_ground_offset_fraction)
        and lets Roblox walk there, then re-uses the same floor-region
        pixel-diff confirmation loop approach_and_enter's WASD path uses.
        Requires click-to-move enabled in Roblox's own settings
        (user-confirmed on, 2026-09-09); set movement.click_to_move: false
        to fall back to the WASD path if not."""
        self._ensure_first_person()

        if not door_type:
            self.logger.warning("approach_and_enter called with no known door type — clicking last-known position anyway.")
        cx, cy = match.center
        if self.click_to_move_click_on_door:
            # User-confirmed 2026-09-09: right-click-to-move accepts a click
            # directly on the door itself — no need to guess an offset
            # ground point below it.
            target = (int(cx), int(cy))
        else:
            ground_y = cy + match.h * self.click_to_move_ground_offset_fraction
            ground_y = max(ground_y, self.frame_h * self.click_to_move_ground_min_fraction)
            ground_y = min(ground_y, self.frame_h * 0.6)
            target = (int(cx), int(ground_y))
        self.logger.info("Click-to-move (%s): walking to '%s' at %s.", self.click_to_move_button, door_type, target)
        input_sim.click_at(*target, button=self.click_to_move_button)
        time.sleep(self.click_to_move_arrival_wait_seconds)

        if self.jump_key:
            input_sim.tap_key(self.jump_key, self.jump_hold_seconds)
        if self.interact_key:
            input_sim.tap_key(self.interact_key)

        floor_crop_before = self._floor_region_crop(self._grab_frame())
        if floor_crop_before is None:
            return

        # Waits and re-checks the "Floor: N" crop, same as the WASD path —
        # but critically does NOT click anywhere else while waiting.
        # Clicking a generic unrelated forward point every tick (the
        # original version) issues a brand new click-to-move command each
        # time, which INTERRUPTS whatever path is already in progress — if
        # click_to_move_arrival_wait_seconds wasn't long enough for a
        # farther-away door, that redirect fired before the character ever
        # actually reached it, sending it off toward the generic point
        # instead and never entering the door at all (live-observed
        # 2026-09-09: closer "combat" doors confirmed fine, farther "elite"
        # doors never registered a floor change, 3 attempts in a row).
        #
        # If there's still no change halfway through the budget, retry once
        # — but NOT at the exact same point. Live debug screenshots
        # (2026-09-09) showed the doors sit on a raised platform at the
        # arena's edge, with the actual lit walkable floor more centered;
        # clicking straight down from a door near the screen edge can land
        # just past the floor's edge into unwalkable space (confirmed: the
        # same door type failed at x=524, then succeeded at x=583 on the
        # very next detection — a 59px difference in x was the difference
        # between failure and success). The retry click is nudged partway
        # toward screen-center at the same y, more likely to land on lit
        # floor than repeating the identical point.
        walked = 0.0
        reached_new_floor = False
        reclicked = False
        while walked < self.walk_to_center_max_seconds:
            time.sleep(self.walk_to_center_step_seconds)
            walked += self.walk_to_center_step_seconds
            floor_crop_now = self._floor_region_crop(self._grab_frame())
            if vision.mean_pixel_diff(floor_crop_before, floor_crop_now) > 1.0:
                self.logger.info("Floor number changed — walking to platform center.")
                reached_new_floor = True
                break
            if not reclicked and walked >= self.walk_to_center_max_seconds / 2:
                retry_x = int(target[0] + (self.frame_w / 2 - target[0]) * 0.5)
                retry_target = (retry_x, target[1])
                self.logger.info("No floor change yet — retrying '%s' closer to center at %s.", door_type, retry_target)
                input_sim.click_at(*retry_target, button=self.click_to_move_button)
                reclicked = True

        self.self_tuner.record_walk_confirm_result(reached_new_floor)

        if reached_new_floor:
            self._click_walk_forward()
            time.sleep(self.walk_to_center_extra_seconds)
        else:
            self.logger.info(
                "Floor number didn't change within %.1fs of extra walking — stopping here to be safe.",
                self.walk_to_center_max_seconds,
            )

    def approach_and_enter(self, match: vision.Match) -> None:
        """Turns the CAMERA (arrow keys) to actually face the door dead
        center before walking — not a single fixed-duration strafe guess
        committed to blindly. Always steered by local pixel-offset matching
        (_align_locally), not the AI (_align_with_ai) — even when
        door_detection.method is "ai_hybrid" (that setting still governs
        DOOR-TYPE classification in detect_door(), just not alignment
        steering).

        This used to prefer _align_with_ai whenever available. Reverted
        (2026-09-08) after live testing — with the GUI tracer now actually
        tracking the door's real position during alignment (previously it
        stayed frozen, a separate bug also fixed today), a captured
        sequence showed the AI call "left" 10 times in a row while the
        door visibly slid from left-of-center to almost fully off-screen
        to the RIGHT — i.e. it overshot badly and never noticed, because a
        single categorical word per attempt carries no magnitude and
        apparently isn't reliably re-judging the CURRENT frame's true
        offset each time. _align_locally doesn't have this failure mode:
        it measures an actual pixel offset via template match every
        attempt, so an overshoot flips the measured sign immediately and
        the halving-step logic reacts to it, instead of politely
        continuing to say "left" while the door sails past center. This
        directly matches what was reported live: "it moves the camera off
        target... instead of on the door.\"

        Click-to-move (2026-09-09): if movement.click_to_move is enabled
        (the default now that click-to-move is confirmed on in-game), this
        whole camera-alignment approach is skipped entirely — see
        _approach_and_enter_clickmove."""
        self._ensure_game_focus()

        if self.click_to_move:
            self._approach_and_enter_clickmove(match, self.last_match_name)
            return

        door_type = self.last_match_name
        if not door_type:
            # Shouldn't normally happen (last_match_name is set by whatever
            # detect_door() call produced this match) — walk with whatever
            # heading we've already got rather than guessing further. Not
            # fed to self_tuner: there's no real "alignment attempt" here
            # to judge.
            self.logger.warning("approach_and_enter called with no known door type — skipping alignment.")
        else:
            converged = self._align_locally(door_type, match)
            self.self_tuner.record_alignment_result(converged)

        floor_crop_before = self._floor_region_crop(self._grab_frame())

        self.logger.info("Walking into door for %.1fs.", self.walk_through_seconds)
        input_sim.hold_key_for(self.forward_key, self.walk_through_seconds)

        if self.jump_key:
            input_sim.tap_key(self.jump_key, self.jump_hold_seconds)

        if self.interact_key:
            input_sim.tap_key(self.interact_key)

        # Keep walking (in short steps, re-checking the "Floor: N" HUD crop
        # each time) until it visibly changes — confirms the room
        # transition really happened, not just "walked forward for a fixed
        # guess and hoped" — then a bit further to reach the new platform's
        # center where the floor number is painted on the ground. Capped
        # by walk_to_center_max_seconds so a missing floor_region (or a
        # transition that never renders a change, edge case) can't walk
        # forever. Plain pixel diff, not OCR — see _floor_region_crop for
        # why (Tesseract can't read this font at all, confirmed live).
        if floor_crop_before is not None:
            walked = 0.0
            reached_new_floor = False
            while walked < self.walk_to_center_max_seconds:
                input_sim.hold_key_for(self.forward_key, self.walk_to_center_step_seconds)
                walked += self.walk_to_center_step_seconds
                floor_crop_now = self._floor_region_crop(self._grab_frame())
                if vision.mean_pixel_diff(floor_crop_before, floor_crop_now) > 1.0:
                    self.logger.info("Floor number changed — walking to platform center.")
                    reached_new_floor = True
                    break

            self.self_tuner.record_walk_confirm_result(reached_new_floor)

            if reached_new_floor:
                input_sim.hold_key_for(self.forward_key, self.walk_to_center_extra_seconds)
            else:
                self.logger.info(
                    "Floor number didn't change within %.1fs of extra walking — stopping here to be safe.",
                    self.walk_to_center_max_seconds,
                )

    def scan_tick(self) -> None:
        """Fallback only: bounded, STATIONARY look-around sweep (2026-09-09
        rewrite) — turns the CAMERA (never A/D, never forward) in
        scan_sweep_directions quick steps, checking for a door after each
        one and returning the instant it appears rather than always
        completing the full sweep. Walking forward while blindly turning
        (the old behavior) risked running into obstacles while searching;
        this only walks once a door is actually found and approach_and_enter
        takes over. Used while waiting/searching, never during
        approach_and_enter itself. Camera turn is a right-click-drag by
        default (camera_turn_method: "mouse") since that's Roblox's native
        camera control; set to "keys" to use the arrow-key binding instead."""
        self._ensure_game_focus()
        for step in range(self.scan_sweep_directions):
            if self.camera_turn_method == "mouse":
                input_sim.drag_camera(self.camera_drag_pixels, self.scan_sweep_turn_seconds)
            else:
                input_sim.tap_key(self.camera_right_key, self.scan_sweep_turn_seconds)
            time.sleep(self.scan_sweep_pause_seconds)
            if self.detect_door(self._grab_frame()) is not None:
                self.logger.info(
                    "Scan sweep found '%s' after %d/%d turn(s).",
                    self.last_match_name, step + 1, self.scan_sweep_directions,
                )
                return

    def stop_all_movement(self) -> None:
        input_sim.release_all(
            self.forward_key, self.back_key, self.left_key, self.right_key,
            self.camera_left_key, self.camera_right_key,
        )
