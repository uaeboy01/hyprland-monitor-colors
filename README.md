# Monitor Colors

Independent saturation, contrast, gamma, hue, white balance, and RGB controls for
monitors managed by Hyprland's Lua configuration. The app builds ICC profiles
using Little CMS and attaches them to existing monitor rules.

These are creative color adjustments, not measured display calibration. The app
is intended for the SDR desktop; HDR behavior has not been validated.

![Monitor Colors preview](docs/preview.png)

## Requirements

- Linux and a running Hyprland session with Lua monitor rules and ICC support.
- `hyprctl` available in your command path.
- Python 3.10 or newer, NumPy, PySide6, and the Little CMS 2 shared library.

The original installed app was used on CachyOS with Hyprland 0.56.2 and NVIDIA
615.78.08. The shareable version adds configurable monitor selection; a live
Apply from this packaged version has not been tested. NVIDIA is not required
by the app's code, but other graphics stacks have not been tested here.

## Install and run

On Arch Linux / CachyOS, install dependencies:

```bash
sudo pacman -S --needed python python-numpy pyside6 lcms2
```

From this repository, run:

```bash
python monitor_colors.py --list-monitors
```

This read-only command prints connected connector-to-model pairs. Create
`~/.config/monitor-colors/monitors.json` using the exact names it prints:

```json
{
  "DP-1": "YOUR EXACT MONITOR MODEL",
  "HDMI-A-1": "YOUR OTHER MONITOR MODEL"
}
```

List only the monitors you want the app to manage. Connector names can change
after reconnecting displays; update the mapping if that happens.

Your selected monitors must already have rules in
`~/.config/hypr/config/monitors.lua`, with one connector-based `hl.monitor`
block per monitor. Preserve your existing mode, position, and scale:

```lua
hl.monitor({
    output = "DP-1",
    -- Keep your existing mode, position, scale, and other fields here.
})
```

The configuration editor expects the opening `hl.monitor({` and closing `})`
on their own lines, as shown. It does not edit description-based selectors,
computed monitor rules, or arbitrary Lua layouts. Do not replace a working
monitor configuration with the abbreviated example above.

Then launch the app:

```bash
python monitor_colors.py
```

Choose a monitor, adjust the sliders, and press **Apply to this monitor**.
Reset returns the controls to their defaults; press Apply to save that reset.
Revert discards the current unsaved edits.

### Optional virtual environment / command installation

Little CMS and Hyprland remain system dependencies. For Python dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python monitor_colors.py
```

`reqs.txt` is an alias for `requirements.txt`. To also install the
`monitor-colors` command in the active virtual environment:

```bash
python -m pip install .
monitor-colors
```

## Command-line controls

```bash
# Read saved settings; does not verify that those profiles are active.
python monitor_colors.py --show

# Retain other saved controls and change saturation on one monitor.
python monitor_colors.py --monitor DP-1 --saturation 110 --apply

# Use alternate mapping and monitor-rule files.
python monitor_colors.py --monitor-map /path/to/monitors.json --config /path/to/monitors.lua
```

Available controls: `--saturation`, `--contrast`, `--gamma`, `--hue`,
`--temperature`, `--red`, `--green`, and `--blue`. Run `--help` for options.
If no monitor is specified, the CLI selects the first entry in the mapping.

## Files and display behavior

Applying changes:

1. Generates a cached ICC profile and JSON metadata under
   `~/.local/share/color/icc/monitor-colors/`.
2. Adds or replaces the selected monitor's app-managed `icc` field.
3. Creates `monitors.lua.before-monitor-colors` beside the monitor-rule file
   if that backup does not already exist. It is an initial backup, not a history.
4. Reloads Hyprland, checks reported configuration errors, and compares output
   resolution, refresh rate, position, scale, transform, and disabled state.
5. Attempts to restore the previous configuration if those checks fail.

Apply preserves the existing mode and layout fields in the edited file.
Reloading still reapplies the complete compositor configuration, including rules
from other files. The app also uses `~/.local/state/monitor-colors/apply.lock`
to serialize its own Apply operations. Other monitor managers do not share that
lock.

The default adjustment removes this app's profile assignment. Existing
unmanaged ICC assignments are protected rather than silently overwritten.
Legacy profiles from the original DP-2 saturation utility can still be read.

## Noctalia / monitor-manager conflicts

The Noctalia Hyprland Settings plugin can generate additional monitor rules.
Hyprland selects the last matching rule as a whole; it does not merge an
ICC setting from an earlier rule into a later rule.

The working order on the original setup was:

```lua
require("hyprland-settings")
require("config.monitors")
```

This gives **all** fields in `config.monitors` precedence, including mode,
refresh rate, position, and scale. Later layout changes through the plugin may
therefore be overridden. Keep one deliberate source of authority for each
monitor's complete rule.

**Known limitation:** Apply checks reload success and geometry, but does not
verify that its ICC profile became active. If a later rule overrides the profile,
the app can report success without visible color changes. Reapplying the same
saved controls can also return without reloading because the file is unchanged.

See [the troubleshooting notes](docs/troubleshooting.md) for more detail.

## Tests

Run from the repository root:

```bash
python monitor_colors.py --self-test
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

The self-test checks 15 generated profile transforms through Little CMS.
The 12 unit tests exercise color behavior, isolated configuration transactions,
rollback, explicit monitor mappings, read-only queries, and GUI draft handling.
Tests use temporary files, synthetic monitor rules, and mocked `hyprctl` calls;
they do not apply display settings or reload Hyprland.

See [VALIDATION.md](VALIDATION.md) for what was actually verified.

## Sharing

This repository excludes personal monitor rules, generated profiles, system
logs, application data, and local backups. Review changes before uploading.
No license has been selected; add a license if you want to grant explicit reuse
and redistribution permissions. `pyproject.toml` provides optional pip packaging.

The [NVIDIA idle-power notes](docs/nvidia-idle-power.md) describe a separate
manual driver experiment. The color app does not change driver parameters.
