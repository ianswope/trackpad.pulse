# Changelog

Versions follow semver and live in `manifest.json`, which the panel header, the About page and `status` all read. Every release is tagged `vX.Y.Z`.

## 1.1.0 — 2026-09-14

**Optimize for my hand.** The recorder now keeps one row per touch session for seven days and names the two fingerprints of a wrong curve as sessions end: an **overshoot correction** (a long move answered at once by a short move back) and a **re-stroke** (a long move continued at once in the same direction), plus their scroll equivalents.

- New button on Pointer feel. It fits Start to the speed below which 45% of your movement happens and End to the 90th percentile, then nudges Fast swipes, Precision and Scroll speed by at most 10% a pass when the correction or re-stroke rate in that speed band runs high. Each change comes with its reason and the evidence. Nothing changes until Apply, which goes through Trackpad Plus's journalled path, so Restore previous still works.
- Every applied pass is logged; the next pass judges it by the same rates since it was applied and by the target-practice time, which the curve editor now measures (median seconds from a target appearing to the click).
- A System or Flat profile gets a first fit: a custom curve from the Mac-inspired gains with your Start and End.
- Thin data fits the shape but withholds the gain nudges and says so. Both signals running high at once cancel and say so. A raise that would pass the chart ceiling points at Device scale instead.
- IPC: `optimize` opens Pointer feel with a fresh proposal.
- Recorder: `optimize` and `optimize-applied` actions; `sessions` table; `longMoves`, `corrections`, `restrokes`, `scrollCorrections`, `scrollRestrokes` counters.

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
