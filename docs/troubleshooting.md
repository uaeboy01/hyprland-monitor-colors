# Troubleshooting

## Apply succeeds but nothing changes

Check every matching `hl.monitor` rule, including generated settings files.
In Hyprland 0.56.2, the last matching rule wins as a whole. A later rule without
`icc` can override an earlier rule that has a profile. Moving the color app's
monitor rules later can restore the ICC assignment, but also gives that file
precedence for mode and arrangement.

Do not hand-edit a plugin-generated file as a durable solution unless the plugin
preserves your changes. Noctalia's Hyprland Settings generated file explicitly
warns that its next Apply overwrites manual edits.

The app currently validates configuration loading and geometry, not effective
ICC activation. There is no automatic fix for this conflict in this release.

Source: https://github.com/hyprwm/Hyprland/blob/v0.56.2/src/config/shared/monitor/MonitorRuleManager.cpp

## No configured monitor is connected

Run `python monitor_colors.py --list-monitors` and compare its output to the
mapping JSON. Both connector and model must match exactly. The mapping is read
at startup, so reopen the app after editing it.

## Expected one monitor configuration

The app requires exactly one block in the selected file whose `output` is the
connector name. Description selectors and dynamically generated Lua blocks are
not supported by its text editor. It does not create missing monitor rules.

## Another ICC profile is already configured

The app refuses to overwrite an unmanaged profile. Decide which color-management
tool should own the output before changing that configuration manually.

## Little CMS or Qt cannot load

Install `lcms2` through your distribution. Python requirements do not install
that system shared library. Run the GUI inside the desktop session, not a headless
shell. The `offscreen` Qt setting is for automated GUI tests, not regular use.

## Saved settings changed outside the window

Close and reopen Monitor Colors so it reads the latest saved controls. The app
rejects stale edits rather than silently replacing newer saved adjustments.
