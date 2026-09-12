# Summon Heroes Macro

An **external** input-simulation macro for the Roblox game "Summon Heroes".
It works purely by:

- Taking screenshots of your screen (via `mss`)
- Running OpenCV template/color matching on those screenshots
- Sending OS-level key presses and mouse clicks (via `pynput`/`pyautogui`)

It never injects into the Roblox process, never reads or writes its memory,
and never modifies any game files. That said: **using any automation tool
in Roblox is against Roblox's Terms of Service and can get your account
banned.** Use at your own risk, ideally on an account you're OK losing.

## What it does

1. **Door walking** — detects the glowing door archways on screen (template
   matching, with an optional color-blob fallback) and steers the character
   toward the nearest one with WASD. Doors in this game open just by
   walking into them, so once a door fills enough of the screen the macro
   just walks straight through.
2. **Teleport-to-hero hotkey** — a configurable hotkey (default `F8`) clicks
   the purple "teleport to hero" button, located either by template match or
   a fallback fixed coordinate.
3. **Constant auto-jump** — taps Space on a configurable interval the whole
   time the macro is running.
4. **Watchdog** — a background thread that, every second or so:
   - confirms Roblox is still the foreground window
   - confirms a known HUD element (e.g. the XP bar) is still visible, as a
     proxy for "not stuck in a menu/dialog"
   - compares a small screen crop across recent frames to detect "nothing
     is moving" (stuck)
   - on a stuck condition, nudges left/right and retries a few times; if
     that fails, it **pauses the macro and alerts you** (console message +
     beep) instead of running blind
   - logs everything with timestamps to a rolling log file

## Setup

Runs on **Windows** and **macOS**. Windows gets a packaged, self-updating
`.exe` (see GitHub Releases) — no Python install needed there. macOS has
no prebuilt app (nobody involved in building this has a Mac to build one
on — PyInstaller can't cross-compile between OSes), so it runs from source.

### Windows

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

The app self-elevates (UAC prompt) on startup — Windows blocks simulated
input from a lower-privilege process reaching a higher-privilege window,
so this keeps working even if Roblox itself is ever run as Administrator.

### macOS

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

Before it'll actually see/control anything, grant these once in **System
Settings > Privacy & Security**, for whatever app is running the command
above (Terminal, iTerm, etc. — or the specific Python binary in `venv/`,
if macOS prompts for that instead):

- **Accessibility** — required for `pynput` to send simulated
  mouse/keyboard input and read global hotkeys at all. Without this,
  clicks/keypresses silently do nothing.
- **Screen Recording** — required for `mss` to capture the screen at all.
  Without this, macOS returns blank/black screenshots and every
  template-match will simply fail to find anything.

macOS has no UAC/admin-elevation equivalent, so the app doesn't try to
elevate itself there — just run it normally after granting the two
permissions above (may need to quit and reopen Terminal after granting
them for the change to take effect).

Optional, for Towers mode's OCR-based floor-cleared detection:

```bash
brew install tesseract
```

**Known macOS limitations (not live-tested against a real Mac + Roblox —
no Mac was available while building this; please report back what does/
doesn't work):**
- The in-game overlays (AFK banner, floor-number mirror, PvP ELO overlay,
  Towers tracer box) use a Windows-only Tk transparency trick
  (`-transparentcolor`) to render as see-through, click-through text over
  the game. On macOS this degrades gracefully to a plain opaque window
  instead of crashing, but it will look like a solid box, not transparent
  text, and may block clicks underneath it.
- Camera-turning (`movement.camera_turn_method: "mouse"`, and Towers
  mode's search-sweep) uses a Quartz-based relative-mouse-motion port of
  the same fix Windows needed for Roblox's camera-look to register real
  drag input instead of ignoring it — this is a best-effort port, not a
  confirmed-working one on real macOS + Roblox.
- Self-elevation and the auto-updater are Windows-only concepts and are
  simply skipped on macOS (see `main.py`) — update by pulling the repo /
  redownloading the source yourself.

## Calibrating for your setup

Nothing is hardcoded — `config.yaml` holds every coordinate, template path,
hotkey, and timing value. You already told me your resolution is 2560x1440
fullscreen, which is set as the default `screen.width`/`height`/`game_region`
in `config.yaml`; the rest still needs your own captures since template
matching only works against real pixels from your machine.

### 1. Capture the teleport-to-hero button template

```bash
python tools/capture_template.py templates/teleport_button.png
```

Get the purple button visible in-game, run the command, switch to Roblox
during the 3-second countdown, then drag a tight box around just the button
in the popup window and press Enter. This also prints a fallback pixel
coordinate you can paste into `teleport_button.fallback_coordinate` in
`config.yaml` in case the template ever fails to match (e.g. right after
the UI shifts and before you recapture it).

### 2. Capture door archway template(s)

Same tool, aimed at a door/portal archway:

```bash
python tools/capture_template.py templates/door_archway_1.png
```

Capture it when the archway is at a medium distance (not too close, not a
tiny speck) so template matching has a representative size to match against.
If the game has more than one visual style of door/portal, capture a couple
and list them all under `door_detection.templates` in `config.yaml`.

If template matching turns out unreliable for doors (e.g. they look
different depending on lighting/floor), flip `door_detection.method` to
`"color"` and tune `hsv_lower`/`hsv_upper` to the archway's glow color —
`tools/pick_coordinate.py` combined with any color-picker tool (e.g.
Windows' built-in screenshot/color tool, or `cv2`'s own HSV inspection) can
help you find good bounds.

### 3. Capture a HUD "heartbeat" anchor

Pick something on screen that's always present during normal gameplay and
disappears when you're in a menu or dialog — the XP/tower-level bar at the
top of the screen is a good choice given your UI:

```bash
python tools/capture_template.py templates/xp_bar_anchor.png
```

### 4. Set the watchdog's "stuck" sample region

`watchdog.stuck.sample_region` is a `[x, y, w, h]` box the watchdog repeatedly
crops and diffs to detect "nothing is moving". Use:

```bash
python tools/pick_coordinate.py
```

to read off coordinates while hovering over the game window, and pick a
region roughly centered on the game view (avoiding HUD text, which doesn't
move even when you're walking and would create false "not stuck" readings).

### 5. Double check hotkeys and movement keys

`hotkeys.start` / `hotkeys.stop` / `hotkeys.teleport_to_hero` and
`movement.keys.*` / `movement.jump_key` are all plain config values — edit
them directly in `config.yaml` if any of your bindings differ from the
defaults (`F6`/`F7`/`F8`, WASD, Space).

## Running

```bash
python main.py
```

- **F6** — start/resume
- **F7** — stop / panic (immediately releases every held movement key)
- **F8** — click the teleport-to-hero button

Watch the terminal and `logs/macro.log` for what it's doing and any watchdog
alerts. If it pauses itself (watchdog alert), fix whatever it flagged in the
game, then press F6 again to resume.

## Project layout

```
summon_heroes_macro/
  config.yaml            # all tunables — edit this, not the code
  main.py                 # entry point / main loop
  modules/
    vision.py             # screenshot capture + template/color matching
    input_sim.py           # OS-level key/mouse simulation
    navigation.py           # door detection -> WASD steering
    watchdog.py              # background self-monitoring + recovery
    hotkeys.py                # global start/stop/teleport hotkeys
    logging_setup.py           # rolling log file setup
  tools/
    capture_template.py         # interactive screenshot -> template cropper
    pick_coordinate.py            # live mouse coordinate readout
  templates/                       # your captured template PNGs go here
  logs/                              # rolling macro.log lives here
```

## Extending it

- Multiple door "choice" rooms: the current logic walks to the nearest/best
  detected door. If you want logic that prefers a specific room type (e.g.
  reading room-modifier text via OCR), that'd slot into
  `navigation.DoorNavigator.detect_door` — return whichever match your new
  scoring picks.
- More watchdog checks (e.g. a specific "disconnected" dialog template) can
  be added the same way `hud_anchor` works: capture a template, add a
  matching check in `watchdog.Watchdog.run`.
