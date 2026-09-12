"""
main.py — Summon Heroes macro, entry point.

Two independent automation modes, switchable via GUI tabs, never running at
once (starting one stops the other since they'd fight over the same clicks
and movement keys):

  Story   — dead simple: click the teleport-to-hero button on a fixed
            interval. F6 start, F7 stop.
  Towers  — full door-detection automation: teleport-to-hero while no door
            is visible, walk into the best-ranked door the moment one
            appears, repeat. F8 toggles enable/pause.

Pure external input-simulation — screenshots the screen and sends OS-level
mouse/keyboard events (pyautogui / pynput). Never injects into the Roblox
process, never reads/writes its memory, never touches game files.

Run:
  python main.py       (or double-click the desktop shortcut)
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time

import yaml

# Every relative path in this app (config.yaml, templates/, debug/, logs/)
# assumes cwd is the app folder. That's true when run as `python main.py`
# from this directory, but not guaranteed for a packaged .exe launched via
# a shortcut, drag-and-drop, or from a different working directory — so
# anchor cwd to wherever the running app actually lives before anything
# else loads. sys.executable is the .exe itself when frozen (PyInstaller);
# otherwise it's the Python interpreter, so use this script's own path.
if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))
else:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))


def _check_for_update_and_exit_if_updating() -> None:
    """Runs before elevation — the update check itself (a network request +
    writing into this folder) needs no admin rights, and applying an update
    means replacing this very exe, which should happen from a clean,
    non-elevated relaunch rather than mid-way through the admin one. Only
    matters for the packaged .exe; see modules/updater.py."""
    from modules import updater

    if updater.check_and_apply_update(os.getcwd()):
        sys.exit(0)


def _relaunch_elevated_if_needed() -> None:
    """Windows-only. Windows blocks simulated mouse/keyboard input from a
    lower-privilege process reaching a higher-privilege window (UIPI) — if
    Roblox is ever run as Administrator, an unelevated macro's clicks/keys
    silently do nothing even though the game genuinely has focus. Rather
    than requiring the two to be manually kept in sync, always run
    elevated: check once at startup and, if not already admin, relaunch
    self via the UAC 'runas' verb and exit this (non-elevated) instance.

    macOS/Linux (2026-09-12): no equivalent of UIPI/UAC elevation here —
    permission is handled per-app via System Settings > Privacy & Security
    (Accessibility, for simulated input; Screen Recording, for screenshots),
    granted once to whatever runs main.py (Terminal, or python3 itself),
    not something this process can request or elevate into on its own. See
    README's macOS section."""
    if sys.platform != "win32":
        return

    import ctypes

    if ctypes.windll.shell32.IsUserAnAdmin():
        return

    params = " ".join(f'"{a}"' for a in sys.argv)
    # SW_SHOWNORMAL=1 — same as a normal launch, just elevated.
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, os.getcwd(), 1)
    # ShellExecuteW returns <=32 on failure (e.g. UAC prompt dismissed) —
    # in that case, fall through and keep running non-elevated rather than
    # silently vanishing with no window at all.
    if result > 32:
        sys.exit(0)


_check_for_update_and_exit_if_updating()
_relaunch_elevated_if_needed()

from modules import vision, input_sim, hotkeys, logging_setup, web_gui, towers, story_campaign, pvp_spam, auto_clicker, version


def _deep_update(target: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _apply_resolution_profile(config: dict) -> None:
    """Overwrite the fixed-pixel-coordinate fields (teleport_button,
    pvp_spam, overlay, door_detection.search_region, boss_floor) with the
    entry matching screen.width x screen.height from resolution_profiles,
    so those modes work at resolutions other than the 2560x1440 the base
    values were calibrated at. See config.yaml's resolution_profiles
    comment for the full explanation.

    Keyed by "WIDTHxHEIGHT", not width alone (2026-09-12) — 2560x1600
    (16:10) shares its width with the 2560x1440 (16:9) base but is a
    different aspect ratio, so a width-only key would wrongly treat it as
    "no scaling needed" and reuse the 1440 baseline's y-coordinates
    verbatim on a screen that's actually 160px taller.

    A resolution with no explicit profile falls back to a proportional
    scale: x/width-valued fields scale by screen.width/2560, y/height-valued
    fields scale by screen.height/1440, applied independently — a flat
    single-factor scale (the old behavior) is exactly wrong for any
    non-16:9 resolution like 2560x1600's for the reasons above. Still just
    an approximation, not a calibrated set."""
    profiles = config.get("resolution_profiles", {})
    width = config["screen"]["width"]
    height = config["screen"]["height"]
    reference_width, reference_height = 2560, 1440

    profile = profiles.get(f"{width}x{height}")
    if profile is None and (width, height) != (reference_width, reference_height):
        sx = width / reference_width
        sy = height / reference_height

        def scale_point(pt):
            return [round(pt[0] * sx), round(pt[1] * sy)]

        def scale_region(r):
            return [round(r[0] * sx), round(r[1] * sy), round(r[2] * sx), round(r[3] * sy)]

        profile = {
            "teleport_button": {
                "fallback_coordinate": scale_point(config["teleport_button"]["fallback_coordinate"]),
            },
            "pvp_spam": {
                "click_coordinate": scale_point(config["pvp_spam"]["click_coordinate"]),
                "elo_region": scale_region(config["pvp_spam"]["elo_region"]),
            },
            "overlay": {
                "floor_region": scale_region(config["overlay"]["floor_region"]),
            },
            "door_detection": {
                "search_region": scale_region(config["door_detection"]["search_region"]),
            },
            "boss_floor": {
                "ocr_region": scale_region(config["boss_floor"]["ocr_region"]),
            },
        }

    if profile:
        _deep_update(config, profile)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    _apply_resolution_profile(config)
    return config


class MacroApp:
    def __init__(self, config: dict):
        self.cfg = config
        self.logger = logging_setup.build_logger(config)

        self.capture = vision.ScreenCapture(config["screen"]["game_region"])

        self.teleport_fallback = config["teleport_button"]["fallback_coordinate"]
        self.interval = config["teleport_button"].get("interval_seconds", 2.5)

        self.story_running_event = threading.Event()
        self.towers_running_event = threading.Event()
        self.campaign_running_event = threading.Event()
        self.pvp_running_event = threading.Event()
        self.auto_clicker_running_event = threading.Event()
        self._shutdown_flag = threading.Event()

        self.towers_log_queue: "queue.Queue[str]" = queue.Queue()
        self.towers_automation = towers.TowersAutomation(
            config,
            self.capture,
            self.teleport_to_hero,
            self.towers_running_event,
            self.logger,
            log_callback=self.towers_log_queue.put,
        )

        self.campaign_log_queue: "queue.Queue[str]" = queue.Queue()
        self.story_campaign = story_campaign.StoryCampaign(
            config,
            self.capture,
            self.campaign_running_event,
            self.logger,
            log_callback=self.campaign_log_queue.put,
        )

        self.pvp_log_queue: "queue.Queue[str]" = queue.Queue()
        self.pvp_spam = pvp_spam.PvpSpam(
            config, self.pvp_running_event, self.logger, log_callback=self.pvp_log_queue.put
        )

        self.auto_clicker_log_queue: "queue.Queue[str]" = queue.Queue()
        self.auto_clicker = auto_clicker.AutoClicker(
            config, self.auto_clicker_running_event, self.logger, log_callback=self.auto_clicker_log_queue.put
        )

        self.hotkey_listener = hotkeys.HotkeyListener(
            config,
            self.story_running_event,
            self.towers_running_event,
            logger=self.logger,
            campaign_running_event=self.campaign_running_event,
            pvp_running_event=self.pvp_running_event,
            auto_clicker_running_event=self.auto_clicker_running_event,
        )

        self.gui = web_gui.WebControlPanel(
            self.story_running_event,
            self.towers_running_event,
            config,
            self.towers_automation,
            self.towers_log_queue,
            on_close=self._shutdown_flag.set,
            campaign_running_event=self.campaign_running_event,
            pvp_running_event=self.pvp_running_event,
            pvp_log_queue=self.pvp_log_queue,
            pvp_spam=self.pvp_spam,
            auto_clicker_running_event=self.auto_clicker_running_event,
            auto_clicker_log_queue=self.auto_clicker_log_queue,
            auto_clicker=self.auto_clicker,
            hotkey_listener=self.hotkey_listener,
            config_path="config.yaml",
        )
        # Wire up the hide/show-around-capture hooks now that the GUI exists.
        # Both TowersAutomation's own detection captures AND the navigator's
        # internal captures (the alignment loop, floor-number checks) need
        # this — the tracer overlay is full-screen, so any unprotected
        # capture.grab() call could see the macro's own drawn boxes/lines.
        self.towers_automation.hide_overlays_fn = self.gui.hide_overlays_for_capture
        self.towers_automation.show_overlays_fn = self.gui.show_overlays_after_capture
        self.towers_automation.navigator.hide_overlays_fn = self.gui.hide_overlays_for_capture
        self.towers_automation.navigator.show_overlays_fn = self.gui.show_overlays_after_capture
        self.story_campaign.hide_overlays_fn = self.gui.hide_overlays_for_capture
        self.story_campaign.show_overlays_fn = self.gui.show_overlays_after_capture

    def teleport_to_hero(self) -> None:
        # The teleport button is a fixed 2D HUD element that never actually
        # moves on screen (confirmed across a full night's logs: template
        # matching only ever reported the same coordinate ±1px, which is
        # match-precision noise, not real movement, plus occasional
        # "not matched, using fallback" warnings when it briefly missed).
        # Always clicking the known fixed coordinate directly is strictly
        # more consistent than re-matching it every time — no screen
        # capture needed, no overlay-hide dance, no chance of a miss.
        x, y = self.teleport_fallback
        input_sim.click_at(x, y)

    def _story_click_loop(self) -> None:
        while not self._shutdown_flag.is_set():
            if not self.story_running_event.is_set():
                time.sleep(0.1)
                continue
            self.teleport_to_hero()
            time.sleep(self.interval)

    def run(self) -> None:
        self.hotkey_listener.start()
        threading.Thread(target=self._story_click_loop, daemon=True, name="StoryClickLoop").start()
        threading.Thread(target=self.towers_automation.run, daemon=True, name="TowersLoop").start()
        threading.Thread(target=self.story_campaign.run, daemon=True, name="CampaignLoop").start()
        threading.Thread(target=self.pvp_spam.run, daemon=True, name="PvpSpamLoop").start()
        threading.Thread(target=self.auto_clicker.run, daemon=True, name="AutoClickerLoop").start()
        self.logger.info(
            f"Macro ready (v{version.APP_VERSION}). Story: F6 start / F7 stop. Towers: F8 toggle. "
            f"Campaign: B toggle. PvP: P toggle. Auto Clicker: {self.hotkey_listener.auto_clicker_key.upper()} toggle."
        )

        try:
            self.gui.run()  # blocks on the Tk mainloop until the window closes
        except KeyboardInterrupt:
            self.logger.info("Ctrl+C received, shutting down.")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._shutdown_flag.set()
        self.story_running_event.clear()
        self.towers_running_event.clear()
        self.campaign_running_event.clear()
        self.pvp_running_event.clear()
        self.auto_clicker_running_event.clear()
        self.towers_automation.stop()
        self.story_campaign.stop()
        self.pvp_spam.stop()
        self.auto_clicker.stop()
        self.hotkey_listener.stop()
        self.logger.info("Macro stopped cleanly.")


def _print_macos_permission_status() -> None:
    """macOS only. Printed straight to the terminal (not just the log
    file) as the very first thing on startup, since a stuck-forever hotkey
    or click is otherwise indistinguishable from "permissions are wrong"
    vs. every other thing this app does — this answers that definitively
    by asking the OS directly (see input_sim.macos_permission_status) instead
    of relying on a System Settings toggle that can be out of sync with
    what's actually granted. User-reported (2026-09-13): hotkeys still not
    firing after following the setup steps, even with Input Monitoring
    supposedly granted — this exists to stop guessing and just show the
    real state."""
    if not input_sim.IS_MACOS:
        return
    status = input_sim.macos_permission_status()
    labels = {
        "accessibility": "Accessibility",
        "input_monitoring": "Input Monitoring",
        "screen_recording": "Screen Recording",
    }
    print("\n--- macOS permission check (System Settings > Privacy & Security) ---")
    all_ok = True
    for key, label in labels.items():
        val = status[key]
        if val is True:
            mark = "OK"
        elif val is False:
            mark = "MISSING"
            all_ok = False
        else:
            mark = "UNKNOWN (couldn't check — is pyobjc-framework-Quartz installed?)"
            all_ok = False
        print(f"  [{mark}] {label}")
    if not all_ok:
        print(
            "  -> Grant whatever's marked MISSING to Terminal (or your Python "
            "binary) in System Settings, then fully quit and reopen Terminal — "
            "the app WILL keep running with these missing, it just won't be "
            "able to see the screen and/or send input/hotkeys."
        )
    print("---\n")


def main() -> None:
    _print_macos_permission_status()
    config = load_config()
    app = MacroApp(config)
    app.run()


if __name__ == "__main__":
    sys.exit(main() or 0)
