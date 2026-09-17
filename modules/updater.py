"""
updater.py — checks GitHub Releases for a newer build and self-updates.

Runs once at startup (see main.py), before self-elevation, since the
check itself (a network request + writing to the app's own folder) never
needs admin rights. Only active in the packaged .exe (sys.frozen) — a
`python main.py` dev run always skips it, since there's nothing to
"update" in a source checkout.

Flow: compare APP_VERSION against the repo's latest release tag. If
newer, download that release's SummonHeroesMacroApp.zip asset, extract it
to a temp folder, merge config.yaml (see _merge_config below), then hand
off to a small PowerShell helper that:
  1. waits for this process to exit (Wait-Process — avoids ever touching
     files that are still open/running, which Windows would block anyway)
  2. copies the new exe + templates/ + merged config.yaml over the
     current install directory
  3. relaunches the exe
  4. cleans up the temp folder

User-requested (2026-09-17): "i want to remake the auto updater so it
updates everything everytime we upload it to github because i dont want
people to keep on downloading new versions all the time" — config.yaml
used to be excluded from updates entirely, since most of it (door
detection tuning, resolution-profile coordinates, feature flags) is app
calibration data that needs to reach existing installs same as any other
bugfix, but a few sections genuinely are the user's own setup (hotkey
rebinds, background color, screen/resolution). _merge_config keeps the
newly downloaded config.yaml as the base (so every calibration fix and
new key lands automatically) and copies just the hotkeys/gui/screen
sections over from the user's existing file on top of it, so personal
setup survives while everything else updates. If the merge itself fails
for any reason, this falls back to the old exclude-config.yaml-entirely
behavior — never risk silently corrupting someone's install.

Never raises out to the caller — any failure (no internet, GitHub
unreachable, malformed response) just means "no update happened", the
same as being already up to date. A macro that can't check for updates
should still run normally.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from typing import Optional

from .version import APP_VERSION

_REPO = "Zaql-Infin/summon-heroes-macro"
_API_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_ASSET_NAME = "SummonHeroesMacroApp.zip"


def _parse_version(v: str) -> tuple:
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


# Sections treated as the user's own setup, never silently replaced by an
# update — everything else in config.yaml (door_detection, resolution_
# profiles, skip_farm, teleport_button, features, etc.) is app calibration
# data and SHOULD update same as any other bugfix.
_USER_OWNED_CONFIG_SECTIONS = ("hotkeys", "gui", "screen")


def _merge_config(old_path: str, new_path: str) -> None:
    """Overwrites new_path (the freshly downloaded config.yaml) in place,
    keeping every new key/value EXCEPT that hotkeys/gui/screen come from
    the user's existing config.yaml (old_path) instead — so a rebound
    hotkey, custom background color, or non-default screen resolution
    survives an update, while every calibration/tuning fix in the rest of
    the file reaches the install automatically. Uses ruamel (already a
    dependency — web_gui.py's own config-writing already relies on it) so
    the new file's comments/formatting are preserved, not just its values.
    A key present in the new file but not the old one (e.g. a hotkey for
    a mode that didn't exist yet) is left as the new default — only keys
    the user's file actually has get carried over. No-ops if old_path
    doesn't exist (nothing to preserve) or on any parse error, since a
    half-merged config.yaml is worse than an unmerged one; the caller
    falls back to leaving the user's existing file untouched in that case."""
    if not os.path.isfile(old_path):
        return

    from ruamel.yaml import YAML
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)

    with open(old_path, "r", encoding="utf-8") as f:
        old_doc = yaml_rt.load(f)
    with open(new_path, "r", encoding="utf-8") as f:
        new_doc = yaml_rt.load(f)

    for section in _USER_OWNED_CONFIG_SECTIONS:
        old_section = old_doc.get(section) if old_doc else None
        if not old_section:
            continue
        new_doc.setdefault(section, {})
        for key, value in old_section.items():
            new_doc[section][key] = value

    with open(new_path, "w", encoding="utf-8", newline="\n") as f:
        yaml_rt.dump(new_doc, f)


def check_and_apply_update(app_dir: str, logger: Optional[logging.Logger] = None) -> bool:
    """Returns True if a newer version was found and a self-update relaunch
    was kicked off — the caller must exit immediately in that case (the
    running exe's own file is about to be replaced). Returns False for
    "already up to date" and for any failure along the way."""
    if not getattr(sys, "frozen", False):
        return False

    try:
        req = urllib.request.Request(_API_URL, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)

        latest_tag = data.get("tag_name", "")
        if not latest_tag or _parse_version(latest_tag) <= _parse_version(APP_VERSION):
            return False

        asset_url = next(
            (a.get("browser_download_url") for a in data.get("assets", []) if a.get("name") == _ASSET_NAME),
            None,
        )
        if not asset_url:
            return False

        if logger:
            logger.info(f"Update found: {APP_VERSION} -> {latest_tag}. Downloading...")

        tmp_dir = tempfile.mkdtemp(prefix="shm_update_")
        zip_path = os.path.join(tmp_dir, _ASSET_NAME)
        urllib.request.urlretrieve(asset_url, zip_path)

        extract_dir = os.path.join(tmp_dir, "extracted")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # The release zip wraps everything in one top-level folder — copy
        # ITS contents, not the wrapper folder itself.
        entries = os.listdir(extract_dir)
        source_dir = extract_dir
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            source_dir = os.path.join(extract_dir, entries[0])

        # Merge the user's existing hotkeys/gui/screen settings into the
        # freshly downloaded config.yaml before it gets copied in — if this
        # fails for any reason, fall back to the old safe behavior of just
        # leaving the existing config.yaml alone entirely.
        new_config_path = os.path.join(source_dir, "config.yaml")
        old_config_path = os.path.join(app_dir, "config.yaml")
        config_merged = False
        if os.path.isfile(new_config_path):
            try:
                _merge_config(old_config_path, new_config_path)
                config_merged = True
            except Exception as e:
                if logger:
                    logger.warning(f"config.yaml merge failed, leaving existing config.yaml untouched: {e}")

        exclude_clause = "" if config_merged else ' -Exclude "config.yaml"'

        exe_name = os.path.basename(sys.executable)
        helper_script = os.path.join(tmp_dir, "apply_update.ps1")
        with open(helper_script, "w", encoding="utf-8") as f:
            f.write(
                f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue\n"
                f"Start-Sleep -Seconds 1\n"
                f'Copy-Item -Path "{source_dir}\\*" -Destination "{app_dir}" '
                f'-Recurse -Force{exclude_clause}\n'
                f'Start-Process -FilePath "{os.path.join(app_dir, exe_name)}"\n'
                f'Remove-Item -Path "{tmp_dir}" -Recurse -Force -ErrorAction SilentlyContinue\n'
            )

        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", helper_script],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return True

    except Exception as e:
        if logger:
            logger.warning(f"Update check failed (continuing without update): {e}")
        return False
