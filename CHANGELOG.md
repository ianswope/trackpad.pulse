# Changelog

Versions follow semver and live in `manifest.json`, which the panel header, the About page and `status` all read. Every release is tagged `vX.Y.Z`.

## 1.0.0 — 2026-09-14

First release of Trackpad Pulse, forked from Trackpad Plus 2026.09.13.1.

- **The recorder** (`collectors/trackpad_pulse.py`, `trackpad-pulse.service`): reads the touchpad's evdev node without root when the user may, counts touches, taps, clicks, pointer moves, two-finger scrolls, pinches, three- and four-finger swipes and rejected palms, measures distance and speed in millimetres, keeps a time-weighted finger-speed histogram, and records one row per minute for seven days. Falls back to cursor-only telemetry from the Hyprland socket when the node is closed to the user.
- **Access model**: probes first; offers a udev `uaccess` rule scoped to `ID_INPUT_TOUCHPAD` through one polkit prompt, and removes it (and the ACL it granted) on request. Never asks for the `input` group.
- **The chip**: the bar entry is the pad itself, lit by live finger positions with trails, tap ripples and an idle scan line. No number. Right-click switches the pad off and on.
- **Overview**: today's counts, distance, peak and median speed, active time, an hour/day/week history graph and the finger-speed distribution with the curve's acceleration band shaded.
- **Pointer feel**: David Fano's editor with the recorded finger-speed histogram drawn under the curve and a plain-language reading of how the draft sits against it.
- **Controls**: Trackpad Plus's per-device controls on one screen, two columns, device scale in the open.
- **Touch lab**: every finger live, the pad's physical facts, access state and controls, this week's totals.
- **About**: version from the manifest, links, lineage.
- **IPC**: `open`, `close`, `show`, `hide`, `toggle`, `chooser`, `page`, `enable`, `status`.
- Nothing scrolls: every page fits a 1000-pixel-tall screen.
