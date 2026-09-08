"""
apply_labels.py — non-interactive counterpart to label_doors.py. Takes
labels specified programmatically (e.g. by Claude reviewing each screenshot
directly) instead of via the click-drag GUI, and writes them out in the same
YOLO format / train-val split so train_door_detector.py doesn't care which
tool produced them.

Not meant to be run standalone from the command line — import apply_labels
and call it with a dict of {filename: [(class_name, x, y, w, h), ...]}.
An empty list means "no door visible in this frame" (a valid negative
example, still worth saving).
"""

from __future__ import annotations

import random
from pathlib import Path

import cv2
import yaml


def load_class_names(config_path: str = "config.yaml") -> list[str]:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return [entry["name"] for entry in cfg["door_detection"]["templates"]]


def write_yolo_label(path: Path, boxes: list[tuple[int, int, int, int, int]], img_w: int, img_h: int) -> None:
    lines = []
    for x, y, w, h, cls_id in boxes:
        cx = (x + w / 2) / img_w
        cy = (y + h / 2) / img_h
        nw = w / img_w
        nh = h / img_h
        lines.append(f"{cls_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")
    path.write_text("\n".join(lines), encoding="utf-8")


def apply_labels(
    labels: dict[str, list[tuple[str, int, int, int, int]]],
    raw_dir: str = "dataset/raw",
    dataset_dir: str = "dataset",
    config_path: str = "config.yaml",
    val_fraction: float = 0.15,
    seed: int = 42,
) -> None:
    class_names = load_class_names(config_path)
    name_to_id = {name: i for i, name in enumerate(class_names)}
    raw_dir_p = Path(raw_dir)
    ds_root = Path(dataset_dir)
    random.seed(seed)

    saved = 0
    for filename, boxes_named in labels.items():
        img_path = raw_dir_p / filename
        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"SKIP (not found): {filename}")
            continue
        h, w = frame.shape[:2]

        boxes = []
        for cls_name, x, y, bw, bh in boxes_named:
            if cls_name not in name_to_id:
                print(f"SKIP unknown class '{cls_name}' in {filename}")
                continue
            boxes.append((x, y, bw, bh, name_to_id[cls_name]))

        split = "val" if random.random() < val_fraction else "train"
        img_out_dir = ds_root / "images" / split
        lbl_out_dir = ds_root / "labels" / split
        img_out_dir.mkdir(parents=True, exist_ok=True)
        lbl_out_dir.mkdir(parents=True, exist_ok=True)

        cv2.imwrite(str(img_out_dir / filename), frame)
        write_yolo_label(lbl_out_dir / f"{Path(filename).stem}.txt", boxes, w, h)
        saved += 1

    print(f"Saved {saved} labeled image(s).")
