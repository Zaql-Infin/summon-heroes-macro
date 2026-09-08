"""
train_door_detector.py — trains a YOLOv8n model on the dataset built by
label_doors.py, to replace brittle template matching for door detection.
Handles the door types template-matching kept confusing (Mystery/Combat,
the disabled Lunarium false-positives) because it learns actual visual
features instead of doing raw pixel correlation.

Usage:
    python tools/train_door_detector.py [--epochs 100] [--imgsz 960]

Writes the trained weights to models/door_detector.pt — point
door_detection.yolo_model_path at that file and set
door_detection.method: "yolo" in config.yaml to use it.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import yaml
from ultralytics import YOLO


def load_class_names(config_path: str) -> list[str]:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return [entry["name"] for entry in cfg["door_detection"]["templates"]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="dataset")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=960)
    parser.add_argument("--base-model", default="yolov8n.pt", help="Pretrained weights to fine-tune from.")
    parser.add_argument("--out", default="models/door_detector.pt")
    args = parser.parse_args()

    ds_root = Path(args.dataset).resolve()
    train_dir = ds_root / "images" / "train"
    val_dir = ds_root / "images" / "val"
    if not train_dir.exists() or not any(train_dir.iterdir()):
        print(f"No labeled training images in {train_dir} — run label_doors.py first.")
        return

    class_names = load_class_names(args.config)
    data_yaml_path = ds_root / "data.yaml"
    data_yaml_path.write_text(
        yaml.safe_dump({
            "path": str(ds_root),
            "train": "images/train",
            "val": "images/val" if val_dir.exists() and any(val_dir.iterdir()) else "images/train",
            "names": {i: name for i, name in enumerate(class_names)},
        }),
        encoding="utf-8",
    )
    print(f"Wrote {data_yaml_path}")

    model = YOLO(args.base_model)
    results = model.train(data=str(data_yaml_path), epochs=args.epochs, imgsz=args.imgsz)

    best_weights = Path(results.save_dir) / "weights" / "best.pt"
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(best_weights, out_path)
    print(f"Trained model saved to {out_path}")
    print('Set door_detection.method: "yolo" and door_detection.yolo_model_path '
          f'to "{out_path.as_posix()}" in config.yaml to use it.')


if __name__ == "__main__":
    main()
