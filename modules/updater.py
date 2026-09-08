"""
updater.py — checks GitHub Releases for a newer build and self-updates.

Runs once at startup (see main.py), before self-elevation, since the
check itself (a network request + writing to the app's own folder) never
needs admin rights. Only active in the packaged .exe (sys.frozen) — a
`python main.py` dev run always skips it, since there's nothing to
"update" in a source checkout.

Flow: compare APP_VERSION against the repo's latest release tag. If
newer, download that release's SummonHeroesMacroApp.zip asset, extract it
to a temp folder, then hand off to a small PowerShell helper that:
  1. waits for this process to exit (Wait-Process — avoids ever touching
     files that are still open/running, which Windows would block anyway)
  2. copies the new exe + templates/ over the current install directory
     — deliberately EXCLUDING config.yaml, since that holds the user's
     own screen resolution/hotkey setup and must never be silently
     clobbered by an update
  3. relaunches the exe
  4. cleans up the temp folder

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

        exe_name = os.path.basename(sys.executable)
        helper_script = os.path.join(tmp_dir, "apply_update.ps1")
        with open(helper_script, "w", encoding="utf-8") as f:
            f.write(
                f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue\n"
                f"Start-Sleep -Seconds 1\n"
                f'Copy-Item -Path "{source_dir}\\*" -Destination "{app_dir}" '
                f'-Recurse -Force -Exclude "config.yaml"\n'
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
