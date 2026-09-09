"""
self_tuning.py — the macro watches whether its own actions actually worked
(did alignment converge? did the floor really change after walking?) and
when something keeps failing the SAME way, nudges the relevant setting and
saves it back to config.yaml — so a repeated failure mode gets a little
easier to avoid on the next attempt, and the adjustment persists across
restarts instead of being re-discovered from scratch every session.

This is deliberately not "learning" in the machine-learning sense — no
model weights, no training data. It's a small closed-loop controller: track
a streak of failures of one kind, bump one bounded config value, log it
clearly, reset the streak. Simple, inspectable, and reversible (every
change is visible in config.yaml and in the log).
"""

from __future__ import annotations

import logging
import threading

from ruamel.yaml import YAML

# Round-trip loader/dumper — preserves every comment, blank line, and key
# order in config.yaml. Plain PyYAML's safe_dump would silently strip out
# every explanatory comment in the file (there are a lot of them) on the
# very first auto-save.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)  # matches this file's existing list-indent style


class SelfTuner:
    def __init__(self, config: dict, config_path: str, logger: logging.Logger):
        self.config = config
        self.config_path = config_path
        self.logger = logger
        self._lock = threading.Lock()

        self._align_fail_streak = 0
        self._walk_confirm_fail_streak = 0

        # (section, key, delta, cap, streak_needed) — cap is the ceiling
        # (or floor, if delta is negative) an auto-adjustment will never
        # cross, so this can nudge a value but never run away with it.
        self._ALIGN_BUMP = ("movement", "align_max_attempts", 1, 10)
        # click_to_move (2026-09-09) skips _align_locally and never holds
        # forward_key, so walk_through_seconds does nothing in that mode —
        # bumping it would be a silent no-op. Bump walk_to_center_max_seconds
        # instead (the actual budget the click-to-move confirm-wait loop
        # uses) when click_to_move is on.
        if config.get("movement", {}).get("click_to_move", True):
            self._WALK_BUMP = ("movement", "walk_to_center_max_seconds", 0.5, 8.0)
        else:
            self._WALK_BUMP = ("movement", "walk_through_seconds", 0.3, 5.0)
        self._STREAK_NEEDED = 3

    def record_alignment_result(self, converged: bool) -> None:
        """Call after every approach_and_enter alignment attempt (AI or
        local) with whether it actually reached "centered" before running
        out of attempts."""
        if converged:
            self._align_fail_streak = 0
            return
        self._align_fail_streak += 1
        if self._align_fail_streak >= self._STREAK_NEEDED:
            self._bump(*self._ALIGN_BUMP, reason=f"alignment failed to converge {self._align_fail_streak} times in a row")
            self._align_fail_streak = 0

    def record_walk_confirm_result(self, confirmed: bool) -> None:
        """Call after the post-walk floor-number confirmation check with
        whether it actually detected a real floor increment (vs. hitting
        the timeout with no confirmation)."""
        if confirmed:
            self._walk_confirm_fail_streak = 0
            return
        self._walk_confirm_fail_streak += 1
        if self._walk_confirm_fail_streak >= self._STREAK_NEEDED:
            self._bump(*self._WALK_BUMP, reason=f"floor-change confirmation failed {self._walk_confirm_fail_streak} times in a row")
            self._walk_confirm_fail_streak = 0

    def _bump(self, section: str, key: str, delta: float, cap: float, reason: str) -> None:
        with self._lock:
            current = self.config.setdefault(section, {}).get(key, 0)
            new_val = min(current + delta, cap) if delta > 0 else max(current + delta, cap)
            if new_val == current:
                return  # already at the cap — don't log a no-op change
            new_val = round(new_val, 3)
            self.config[section][key] = new_val
            self.logger.info(
                "[SELF-TUNE] %s.%s: %s -> %s (%s) — saved to config.yaml",
                section, key, current, new_val, reason,
            )
            self._save(section, key, new_val)

    def _save(self, section: str, key: str, new_val) -> None:
        """Edits just the one changed value directly in the file (via a
        fresh round-trip parse), rather than dumping the whole in-memory
        config dict — keeps every comment/blank-line/order in config.yaml
        intact, and avoids clobbering any change made to the file since
        this process last read it."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                doc = _yaml.load(f)
            doc.setdefault(section, {})[key] = new_val
            # newline="\n" — ruamel writes CRLF by default on Windows,
            # which would otherwise turn every future line in this file
            # into a Git-style whole-file diff for no real reason.
            with open(self.config_path, "w", encoding="utf-8", newline="\n") as f:
                _yaml.dump(doc, f)
        except Exception as e:
            self.logger.error("Self-tune save failed (adjustment kept in memory only): %s", e)
