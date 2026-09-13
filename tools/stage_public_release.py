"""
stage_public_release.py — builds the PUBLIC release folder from the dev
copy (2026-09-13, user-requested: "i want 2 versions of the macro a Dev
version and a public version so mine is always the dev ... for the public
version i want tower section disabled till we get it working properly").

Dev (dist/SummonHeroesMacroApp) always keeps every mode on — that's the
copy actively used/tested. This script copies it into release_stage/,
strips anything personal (background image, logs, debug), and patches
ONLY the staged copy's config.yaml to set features.towers_enabled: false
— the dev config on disk is never touched.

Usage (from the project root, after rebuilding the exe into dist/):
    python tools/stage_public_release.py

Then zip release_stage/SummonHeroesMacroApp and attach it to the GitHub
release as usual.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "SummonHeroesMacroApp"
DST = ROOT / "release_stage" / "SummonHeroesMacroApp"

_EXCLUDE_DIRS = {"logs", "debug"}
_EXCLUDE_FILE_NAMES = {"background.png", "background.jpg", "background.jpeg", "background.bmp"}


def _ignore(dir_path: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        full = Path(dir_path) / name
        if full.is_dir() and name in _EXCLUDE_DIRS:
            ignored.add(name)
        elif full.is_file() and name.lower() in _EXCLUDE_FILE_NAMES:
            ignored.add(name)
    return ignored


def main() -> None:
    if not SRC.is_dir():
        raise SystemExit(f"Dev app folder not found: {SRC} — build the exe into dist/ first.")

    if DST.parent.exists():
        shutil.rmtree(DST.parent)
    shutil.copytree(SRC, DST, ignore=_ignore)

    config_path = DST / "config.yaml"
    text = config_path.read_text(encoding="utf-8")
    patched, count = re.subn(r"(?m)^(\s*towers_enabled:\s*)true\b", r"\g<1>false", text, count=1)
    if count != 1:
        raise SystemExit(
            "Couldn't find 'towers_enabled: true' in the staged config.yaml — "
            "check config.yaml's features section wasn't renamed/removed."
        )
    config_path.write_text(patched, encoding="utf-8")

    print(f"Staged public release at {DST}")
    print("Patched features.towers_enabled -> false in the staged config.yaml only.")


if __name__ == "__main__":
    main()
