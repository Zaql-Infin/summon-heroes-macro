"""
vision.py — screen capture and image-based detection.

Everything here is read-only: it grabs pixels off the screen (via mss) and
runs OpenCV template/color matching on them. Nothing in this module ever
touches the Roblox process directly.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import mss
import pytesseract

# pytesseract shells out to the Tesseract binary by name, which only works if
# it's on PATH — point at the default install location explicitly so this
# doesn't depend on PATH having refreshed for whatever process eventually
# runs this. Windows: winget/installer default. macOS (2026-09-12): Homebrew
# default locations (Apple Silicon vs Intel) — `brew install tesseract`, see
# README's macOS section.
for _tesseract_path in (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
):
    if os.path.isfile(_tesseract_path):
        pytesseract.pytesseract.tesseract_cmd = _tesseract_path
        break


@dataclass
class Match:
    x: int          # top-left x of matched region, in game_region-relative coords
    y: int          # top-left y
    w: int
    h: int
    score: float

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)


class ScreenCapture:
    """Thin wrapper around mss for fast repeated screenshots of one region."""

    def __init__(self, region: list[int]):
        # region = [x, y, w, h]
        self.region = {
            "left": region[0],
            "top": region[1],
            "width": region[2],
            "height": region[3],
        }
        self._sct = mss.mss()

    def grab(self) -> np.ndarray:
        """Returns a BGR numpy array (OpenCV-friendly) of the configured region."""
        raw = self._sct.grab(self.region)
        img = np.array(raw)  # BGRA
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def load_template(path: str) -> Optional[np.ndarray]:
    p = Path(path)
    if not p.exists():
        return None
    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    return img


def find_template(frame: np.ndarray, template: np.ndarray, threshold: float) -> Optional[Match]:
    """Single-scale template match. Good enough as long as the UI doesn't
    zoom/scale — for the teleport button and HUD anchors this holds true."""
    if template is None or template.size == 0:
        return None
    if template.shape[0] > frame.shape[0] or template.shape[1] > frame.shape[1]:
        return None

    result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < threshold:
        return None

    h, w = template.shape[:2]
    return Match(x=max_loc[0], y=max_loc[1], w=w, h=h, score=float(max_val))


def find_best_of_templates(frame: np.ndarray, templates: list[np.ndarray], threshold: float) -> Optional[Match]:
    """Runs multiple door templates against the frame, returns the best hit."""
    best: Optional[Match] = None
    for tmpl in templates:
        m = find_template(frame, tmpl, threshold)
        if m and (best is None or m.score > best.score):
            best = m
    return best


# Doors are 3D-world objects, not flat UI: their on-screen size changes with
# camera distance/angle, so a single fixed-size template match (like AHK's
# ImageSearch) misses constantly. Scanning the template at several scales
# fixes that — this is the reason this project moved from AHK to Python.
# Trimmed from 7 to 5 steps for speed (each extra step is a full extra
# matchTemplate pass per template) — still covers close/medium/far.
_DOOR_SCALES = [0.65, 0.85, 1.0, 1.2, 1.4]


def find_template_multiscale(frame: np.ndarray, template: np.ndarray, threshold: float) -> Optional[Match]:
    if template is None or template.size == 0:
        return None

    best: Optional[Match] = None
    th, tw = template.shape[:2]
    for scale in _DOOR_SCALES:
        w = int(tw * scale)
        h = int(th * scale)
        if w < 8 or h < 8 or h > frame.shape[0] or w > frame.shape[1]:
            continue
        resized = cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        result = cv2.matchTemplate(frame, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val >= threshold and (best is None or max_val > best.score):
            best = Match(x=max_loc[0], y=max_loc[1], w=w, h=h, score=float(max_val))
    return best


def find_template_multiscale_near(
    frame: np.ndarray, template: np.ndarray, threshold: float, near_x: float, window: float,
) -> Optional[Match]:
    """Same as find_template_multiscale, but only searches a horizontal band
    around near_x (± window pixels) instead of the whole frame. Needed when
    two doors of the SAME type are visible side by side (e.g. two "Combat"
    gates) — plain find_template_multiscale always returns the single
    globally-best-scoring match, which can silently flip between the two
    near-identical doors from one call to the next as their scores jitter
    slightly, making repeated re-checks during an alignment loop look like
    the target teleported sideways. Restricting the search window to where
    we already know the door is keeps every re-check locked onto the same
    physical door."""
    if template is None or template.size == 0:
        return None

    fh, fw = frame.shape[:2]
    x0 = max(0, int(near_x - window))
    x1 = min(fw, int(near_x + window))
    if x1 <= x0:
        return None
    cropped = frame[:, x0:x1]

    m = find_template_multiscale(cropped, template, threshold)
    if m is None:
        return None
    return Match(x=m.x + x0, y=m.y, w=m.w, h=m.h, score=m.score)


def find_best_of_templates_multiscale(frame: np.ndarray, templates: list[np.ndarray], threshold: float) -> Optional[Match]:
    """Same as find_best_of_templates, but scans each door template across a
    range of scales so distance-to-camera no longer breaks the match."""
    best: Optional[Match] = None
    for tmpl in templates:
        m = find_template_multiscale(frame, tmpl, threshold)
        if m and (best is None or m.score > best.score):
            best = m
    return best


def find_all_named_templates_multiscale(
    frame: np.ndarray, named_templates: list[tuple[str, np.ndarray]], threshold: float
) -> list[tuple[str, Match]]:
    """Runs every (name, template) pair against the frame independently and
    returns every one that matched — e.g. every door type currently visible
    on screen at once, not just whichever scores highest. Lets the caller
    rank by door type rather than by raw match confidence."""
    found: list[tuple[str, Match]] = []
    for name, tmpl in named_templates:
        m = find_template_multiscale(frame, tmpl, threshold)
        if m:
            found.append((name, m))
    return found


def find_color_blob(frame: np.ndarray, hsv_lower, hsv_upper, min_area: float) -> Optional[Match]:
    """Fallback door detector: looks for the largest blob within an HSV range
    (tuned to the glowing archway color) and returns its bounding box."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lower = np.array(hsv_lower, dtype=np.uint8)
    upper = np.array(hsv_upper, dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    if area < min_area:
        return None

    x, y, w, h = cv2.boundingRect(largest)
    return Match(x=x, y=y, w=w, h=h, score=float(area))


def mean_pixel_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Used by the watchdog's stuck-detector: mean absolute diff between two
    same-sized crops. Low value across many frames => nothing is moving."""
    if a.shape != b.shape:
        return 999.0
    diff = cv2.absdiff(a, b)
    return float(np.mean(diff))


def crop(frame: np.ndarray, region: list[int]) -> np.ndarray:
    """region is [x, y, w, h] relative to the frame itself."""
    x, y, w, h = region
    return frame[y:y + h, x:x + w]


def ocr_text(frame: np.ndarray, region: Optional[list[int]] = None, scale: float = 1.0) -> str:
    """Reads whatever text is in the given region. Tested against live
    frames: downscaling to 0.75x already drops "Floor" from a clean read
    and 0.6x drops everything — this HUD text isn't large/bold enough to
    survive it, unlike the floor_cleared banner's giant lettering. Full
    resolution still only costs ~0.4s on a 2560x1440 region, so there's no
    real reason to trade accuracy for it.

    --psm 6 ("assume a single uniform block of text") matters more than
    resolution — Tesseract's default page-segmentation mode tries to infer
    a document-like column/paragraph layout, which a busy 3D game scene
    with scattered floating text doesn't have. Verified against known-good
    frames: with the default PSM, "CLEARED" was missed even when clearly
    on screen; with --psm 6 it was found consistently across every test
    frame checked.

    Reading actual text sidesteps the whole class of problems template
    matching has with banners that fade/scale in and out — there's no
    threshold to tune, it either reads the word or it doesn't."""
    img = crop(frame, region) if region else frame
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if scale != 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return pytesseract.image_to_string(gray, config="--psm 6")
