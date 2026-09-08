"""
ai_vision.py — AI-assisted door-type identification, to replace pure pixel
template matching for the "which room type is on screen" decision. Template
matching kept breaking on things it has no real understanding of: per-floor
biome reskins changing the stone archway color/texture (even though the
icon itself was unchanged), and a tight icon-only crop occasionally
matching an unrelated texture (a smoke/dust particle effect) at a
confident-looking score. A vision model actually looks at the image
semantically, so it isn't fooled by either.

What it's NOT used for: precise pixel coordinates. Tested against known
frames, coordinates from a text-described bounding box were off by 250-440px
in a 2560px-wide frame — good enough to know "there's a door around here"
but not good enough for the strafing logic, which needs a real position.
So this module only answers "which door type (if any) should I target" —
navigation.py then runs ordinary local template matching for JUST that one
confirmed type to get a precise location, with no other type competing for
priority (which was the whole source of the cross-type false-positive
confusion, e.g. Combat's badge matching Merchant's).
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

import cv2
import numpy as np

try:
    import anthropic
except ImportError:
    anthropic = None


class AIDoorClassifier:
    def __init__(self, config: dict, logger: logging.Logger):
        ai_cfg = config.get("ai_vision", {})
        self.enabled = ai_cfg.get("enabled", False)
        self.model = ai_cfg.get("model", "claude-haiku-4-5-20251001")
        self.logger = logger
        self._client = None

        if not self.enabled:
            return

        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        api_key = os.environ.get(ai_cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
        if anthropic is None:
            self.logger.warning("ai_vision enabled but the 'anthropic' package isn't installed — disabling.")
            self.enabled = False
        elif not api_key:
            self.logger.warning(
                "ai_vision enabled but %s isn't set (check .env) — disabling.",
                ai_cfg.get("api_key_env", "ANTHROPIC_API_KEY"),
            )
            self.enabled = False
        else:
            self._client = anthropic.Anthropic(api_key=api_key)

    def list_visible_types(self, frame: np.ndarray, valid_types: list[str]) -> list[str]:
        """Asks the model which of valid_types are visible on a door/gate in
        this frame — perception only, not the priority decision. Tested:
        asking the model to also apply a stated priority order and pick
        just one was unreliable (it picked a present-but-lower-priority
        type over an equally-visible higher-priority one despite an
        explicit instruction). Picking from what's actually visible is
        exactly what navigation.py's existing _rank_of logic already does
        correctly for template-matching candidates, so reuse that instead
        of trusting the model to re-implement it. Returns [] if none
        visible or anything goes wrong — callers should treat that exactly
        like "no door detected" (safe to keep searching), never as an error
        to crash on."""
        if not self.enabled or self._client is None:
            return []

        try:
            # Downscale before sending — full 2560x1440 costs meaningfully
            # more in vision tokens (both $ and latency) than a door icon
            # actually needs to be legible; 1280px wide is still plenty to
            # read a small badge glyph clearly.
            h, w = frame.shape[:2]
            if w > 1280:
                scale = 1280 / w
                frame = cv2.resize(frame, (1280, int(h * scale)), interpolation=cv2.INTER_AREA)

            ok, buf = cv2.imencode(".png", frame)
            if not ok:
                return []
            img_b64 = base64.b64encode(buf.tobytes()).decode()

            type_list = ", ".join(valid_types)
            prompt = (
                "This is a screenshot from a Roblox tower-climbing game. Doors/gates on screen each "
                f"show a room-type icon and label. The valid types are: {type_list}.\n\n"
                "Which of these types are CURRENTLY visible on a door in this image? A door's icon may "
                "be absent/blank (no type chosen yet), or no door may be in view at all (e.g. mid-combat).\n\n"
                "Respond with ONLY a comma-separated list of the visible type words (e.g. \"chest, combat\"), "
                'or "none" if none are visible. Nothing else.'
            )

            resp = self._client.messages.create(
                model=self.model,
                max_tokens=30,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            answer = resp.content[0].text.strip().lower()
            found = [t.strip() for t in answer.split(",")]
            return [t for t in found if t in valid_types]

        except Exception as e:
            # Never let an API hiccup (rate limit, network blip, bad key)
            # take down door detection — same "treat as no door found, keep
            # going" contract as a genuine miss.
            self.logger.warning("AI door classification failed: %s", e)
            return []

    def judge_alignment(self, frame: np.ndarray, door_type: str) -> str:
        """Steers approach_and_enter's alignment loop: asks the model
        whether the named door is left of screen-center, right of it,
        already centered enough to walk straight into, or has gone out of
        view. A categorical left/right/centered call plays to what a
        vision model is actually reliable at — unlike asking it for exact
        pixel coordinates, which measured 250-440px off on a 2560px frame
        (see list_visible_types's docstring) and is nowhere near precise
        enough to steer by directly.

        Returns one of "left" (turn camera left), "right" (turn camera
        right), "centered" (close enough — stop turning, walk forward), or
        "lost" (door no longer visible — proceed with best-known heading).
        Any failure also returns "lost", same fail-safe contract as
        list_visible_types returning []."""
        if not self.enabled or self._client is None:
            return "lost"

        try:
            h, w = frame.shape[:2]
            if w > 1280:
                scale = 1280 / w
                frame = cv2.resize(frame, (1280, int(h * scale)), interpolation=cv2.INTER_AREA)

            ok, buf = cv2.imencode(".png", frame)
            if not ok:
                return "lost"
            img_b64 = base64.b64encode(buf.tobytes()).decode()

            prompt = (
                f"This is a screenshot from a Roblox tower-climbing game. Find the '{door_type}' door/gate "
                "in this image (it has a matching icon and label).\n\n"
                "Compare its horizontal position to the CENTER of the image (not the whole frame edge-to-"
                "edge — just whether it's left of center, right of center, or already close to dead center).\n\n"
                'Respond with ONLY one word: "left" if the door is left of center (camera needs to turn '
                'left to face it), "right" if it\'s right of center, "centered" if it\'s already close '
                'enough to dead center to walk straight into, or "lost" if you cannot find that door '
                "in this image at all. Nothing else."
            )

            resp = self._client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            answer = resp.content[0].text.strip().lower().strip(".")
            if answer in ("left", "right", "centered", "lost"):
                return answer
            return "lost"

        except Exception as e:
            self.logger.warning("AI alignment judgment failed: %s", e)
            return "lost"
