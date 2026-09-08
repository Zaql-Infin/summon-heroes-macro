"""
label_doors.py — interactive bounding-box labeler for the door-detector
training set. Reads raw screenshots from tools/capture_dataset.py's output,
lets you draw a box around each visible door and tag its type, and writes
YOLO-format labels ready for train_door_detector.py.

Class list comes straight from config.yaml's door_detection.templates, so
it always matches what the rest of the macro already knows about door
types — no separate class list to keep in sync.

Usage:
    python tools/label_doors.py [--raw dataset/raw]

Per image:
  1-9        select the class for the NEXT box you draw (shown in the title
             bar and printed to the console)
  b          draw a box for the currently-selected class (opens the
             click-drag selector; press Enter/Space to confirm, C to cancel)
  u          undo the last box drawn on this image
  n          save this image's labels (even if zero boxes — a doorless
             frame is a useful negative example) and move to the next image
  s          skip this image entirely (not saved, revisit later)
  q          quit (progress so far is already saved per-image)

Already-labeled images (a matching .txt already exists) are skipped
automatically, so you can stop and resume this across sessions.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import cv2
import yaml

_DISPLAY_MAX_W = 1600
_DISPLAY_MAX_H = 900


def load_class_names(config_path: str) -> list[str]:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    names = [entry["name"] for entry in cfg["door_detection"]["templates"]]
    return names


def draw_overlay(frame, boxes: list[tuple[int, int, int, int, int]], class_names: list[str]):
    img = frame.copy()
    for x, y, w, h, cls_id in boxes:
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(img, class_names[cls_id], (x, max(0, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    return img


def write_yolo_label(path: Path, boxes: list[tuple[int, int, int, int, int]], img_w: int, img_h: int) -> None:
    lines = []
    for x, y, w, h, cls_id in boxes:
        cx = (x + w / 2) / img_w
        cy = (y + h / 2) / img_h
        nw = w / img_w
        nh = h / img_h
        lines.append(f"{cls_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default="dataset/raw", help="Folder of raw screenshots to label.")
    parser.add_argument("--dataset", default="dataset", help="Output dataset root.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--val-fraction", type=float, default=0.15)
    args = parser.parse_args()

    class_names = load_class_names(args.config)
    print("Classes (press the matching digit to select):")
    for i, name in enumerate(class_names, start=1):
        print(f"  {i}: {name}")

    raw_dir = Path(args.raw)
    images = sorted(raw_dir.glob("*.png"))
    if not images:
        print(f"No images found in {raw_dir}. Run capture_dataset.py first.")
        return

    ds_root = Path(args.dataset)
    random.seed(42)

    remaining = []
    for img_path in images:
        # Already labeled in either split -> skip (lets this resume across sessions).
        stem = img_path.stem
        if (ds_root / "labels" / "train" / f"{stem}.txt").exists() or \
           (ds_root / "labels" / "val" / f"{stem}.txt").exists():
            continue
        remaining.append(img_path)

    print(f"{len(remaining)} of {len(images)} images left to label.")

    win = "label_doors — 1-9 select class, b draw box, u undo, n next, s skip, q quit"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    for idx, img_path in enumerate(remaining):
        frame = cv2.imread(str(img_path))
        if frame is None:
            continue
        h, w = frame.shape[:2]
        scale = min(_DISPLAY_MAX_W / w, _DISPLAY_MAX_H / h, 1.0)

        boxes: list[tuple[int, int, int, int, int]] = []  # full-res coords
        current_class = 0

        while True:
            display = draw_overlay(frame, boxes, class_names)
            if scale != 1.0:
                display = cv2.resize(display, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            cv2.setWindowTitle(
                win,
                f"[{idx + 1}/{len(remaining)}] {img_path.name} | class: {class_names[current_class]} "
                f"| {len(boxes)} box(es) drawn",
            )
            cv2.imshow(win, display)
            key = cv2.waitKey(0) & 0xFF

            if ord("1") <= key <= ord("9"):
                sel = key - ord("1")
                if sel < len(class_names):
                    current_class = sel
                    print(f"Selected class: {class_names[current_class]}")

            elif key == ord("b"):
                roi_display = cv2.selectROI(win, display, showCrosshair=True)
                rx, ry, rw, rh = roi_display
                if rw > 0 and rh > 0:
                    # Map back from display-scaled coords to full-res coords.
                    fx, fy, fw, fh = (int(rx / scale), int(ry / scale), int(rw / scale), int(rh / scale))
                    boxes.append((fx, fy, fw, fh, current_class))
                    print(f"Added {class_names[current_class]} box at ({fx},{fy}) {fw}x{fh}")

            elif key == ord("u"):
                if boxes:
                    removed = boxes.pop()
                    print(f"Undid {class_names[removed[4]]} box")

            elif key == ord("n"):
                split = "val" if random.random() < args.val_fraction else "train"
                img_out_dir = ds_root / "images" / split
                lbl_out_dir = ds_root / "labels" / split
                img_out_dir.mkdir(parents=True, exist_ok=True)
                lbl_out_dir.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(img_out_dir / img_path.name), frame)
                write_yolo_label(lbl_out_dir / f"{img_path.stem}.txt", boxes, w, h)
                print(f"Saved {len(boxes)} box(es) -> {split}/{img_path.name}")
                break

            elif key == ord("s"):
                print("Skipped (not saved).")
                break

            elif key == ord("q"):
                cv2.destroyAllWindows()
                print("Quit.")
                return

    cv2.destroyAllWindows()
    print("All images labeled.")


if __name__ == "__main__":
    main()
