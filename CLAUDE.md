# CLAUDE.md — Trackpad Pulse

Working notes for an AI session on this repository. Read this before touching
anything; most of it was learned the hard way.

## What this is

Trackpad Pulse (`nixfred.trackpad-pulse`) is an Omarchy bar widget: a live
trackpad on the bar and a dashboard behind it. It is built **on top of**
Trackpad Plus by David Fano, which is itself built on Andrew Kent's
omarchy-touchpad-widget. Two halves, deliberately separate:

- **Settings** are Trackpad Plus, kept whole: `trackpads.py`, `Curve.js`,
  `Model.js`, `CurveEditor.qml`, `touchpad-state`, `touchpad-sensitivity`, and
  the action queue / debounce / stale-read logic at the root of `Panel.qml`.
  His tests (`test_trackpads.py`, `test_install.py`, `tst_curve.qml`,
  `test-selection.js`) still pass. Do not "improve" that code; fix it upstream
  or wrap it. The one deliberate exception (1.7.0, Fred's call, "this is our
  project"): device detection for every Mac and the Magic Trackpad's own group
  (`UNNAMED_TOUCHPADS`, `APPLE_BUILTIN`, state version 5), plus `Curve.presetFor`.
- **Telemetry and everything new** is ours: `collectors/trackpad_pulse.py`
  (the recorder and all its actions), `Pulse.js`, `TrackpadChip.qml`,
  `TouchHistoryGraph.qml`, `SpeedHistogram.qml`, `HeatMap.qml`, `install.py`,
  `test_collector.py`, `test_ipc.py`, and the pages in `Panel.qml`.

Upstream is https://github.com/davefano/omarchy-trackpad-plus. **Never push,
tag or release to it.** Everything goes to https://github.com/nixfred/trackpad.pulse.
The clone keeps `upstream` fetch-only with its push URL set to `DISABLED`; keep
it that way, and pass `-R nixfred/trackpad.pulse` to `gh release`, because `gh`
resolves a fork's default repo to the parent.

## Architecture in one breath

The recorder (`trackpad-pulse.service`, user scope, Python 3 stdlib only) opens
the touchpad's evdev node when the user may, classifies every touch session
(tap, move, scroll, pinch, swipe3, swipe4, palm), measures distance and speed in
millimetres from the kernel's axis resolution, keeps a time-weighted finger-speed
histogram, flags overshoot corrections and re-strokes as sessions end, records a
row per minute and a row per day in `history.sqlite3`, and writes:

| File | Where | Rate |
|---|---|---|
| `snapshot.json` | `$XDG_STATE_HOME/trackpad-pulse/` | 1–5 s |
| `history.json` | same | per minute |
| `today.json` | same | with the snapshot |
| `hint.json` | same | every 10 min (the standing optimizer check) |
| `live.json` | `$XDG_RUNTIME_DIR/trackpad-pulse/` (tmpfs) | 20 Hz while touched |

The panel reads those through `FileView` and never writes them. Panel → recorder
calls are one-shot `python3 collectors/trackpad_pulse.py <action> [json]`
through `bounded()` (timeout-wrapped), and the recorder answers one JSON line.
The panel never holds a command line: gestures and links are ids, the recorder
owns the catalogue and the Lua.

libinput's custom curve x axis is device speed in units per millisecond;
touchpad deltas are normalized to 1000 dpi, so **1 unit/ms = 25.4 mm/s** and the
editor's 0–4 graph spans about 0–102 mm/s. That constant (`MM_PER_UNIT_MS`) is
what lets the finger-speed histogram sit under the curve.

## Rules that are not negotiable

1. **Nothing scrolls.** Every page fits a 1000-px-tall screen. `KeyboardPanel`
   gets the content's real height; there is no `Flickable` or `ScrollView`
   around a page. Width is the remedy. Check `contentNeeded` against
   `availableHeight` in `status` on every page after a change.
2. **No `shadowBlur` on any Canvas.** It rasterises on the GUI thread and froze
   the whole bar. Glow is a few widening, fading strokes. Every chip coalesces
   paints onto one timer at 20 fps or less; the live file is 20 Hz.
3. **One change a pass, and the next pass judges it.** `propose()` returns a
   single change (the first fit is the one exception) plus `queued`; the last
   applied change is judged once by `judge()` against its `watch` spec and the
   verdict is written into the log by `settle()`, never recomputed. An undo is
   a proposal like any other, carrying the logged reason. Do not add a second
   change to a pass, and do not judge silently.
4. **Never auto-apply a setting.** Optimize proposes; the user presses Apply,
   and Apply goes through Trackpad Plus's journalled `applyPointerFeel`, so
   Restore previous keeps working. Gestures apply on selection because that is
   what the user chose, but suggested defaults are shown first and applied only
   on request.
5. **Access model.** Omarchy strips users from the `input` group on purpose.
   Never ask for it. The recorder probes; the panel offers a udev `uaccess` rule
   scoped to `ID_INPUT_TOUCHPAD` through one polkit prompt, and revokes it plus
   the ACL. Root scripts run under `umask 022`; revoke strips `u:$PKEXEC_UID`.
6. **Root functions in `Panel.qml` stay at two-space indentation.**
   `test-selection.js` executes them out of the file with a regex.
7. **Every screenshot is looked at before it is committed.** Crop to the panel,
   open the file, read it. A batch of images is N decisions.
8. **Version in `manifest.json` only**, semver, a `CHANGELOG.md` entry and a
   `vX.Y.Z` tag on every behaviour change. Docs-only commits need no bump.

## Tests

```sh
python3 test_collector.py      # classifier, fingerprints, optimizer, windows, gestures
python3 test_trackpads.py      # Trackpad Plus backend
node test-selection.js         # Panel.qml queue logic, Pulse.js readouts
git add -A && python3 test_install.py
python3 test_ipc.py            # the real IpcHandler block in an offscreen shell
python3 lint-qml.py            # every warning fails except the listed host-metadata gaps
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QT_QUICK_CONTROLS_STYLE=Basic \
  /usr/lib/qt6/bin/qmltestrunner -input tst_curve.qml
```

New QML must lint clean with no exceptions. `Pulse.js` must stay loadable by
node: no `.pragma library`.

## Deploying on a machine

`python3 install.py` publishes the payload (see `PAYLOAD` in it; add any new
file there), starts the recorder, places the widget through the shell, and
disables Trackpad Plus and the original widget. Then `omarchy restart shell`:
the shell's hot reload of a changed plugin directory leaves a stale IPC
handler behind ("Handler was registered but will not be used"), and the
restart script gives up after two seconds while a big shell takes ten, so ping
`omarchy-shell shell ping` yourself. The installer's reload has been seen to
drop the widget into the center section; put it back with
`omarchy-shell shell moveBarWidget nixfred.trackpad-pulse '{"section":"right","index":3}'`.

`omarchy plugin add` runs no installer. The panel covers that: once per load,
a snapshot still stale after five seconds runs `ensure-service`, which starts
the unit unless the `recorder-stopped` marker (left by Stop the recorder) is
there. Machines installed that way sat at RECORDER OFFLINE before 1.6.1.

A recorder release that adds a counter must merge over the previous release's
`today.json` (`_load_today` does); the 1.1.0 recorder crash-looped on exactly
that. The daemon's loop survives a bad tick now, but the journal
(`journalctl --user -u trackpad-pulse.service`) is the first place to look
when numbers stop moving.

## Per pad: identity, size, preset, history

A pad's `device` is its Trackpad Plus group, got by passing `hypr_name()` (Hyprland's
deviceNameToInternalString: lowercase, space/newline/comma → `-`, `/` kept)
through `trackpads.group_devices`, so telemetry and settings name the same pad.
Size comes from the kernel resolution, else udev's `ID_INPUT_WIDTH_MM`, else
libinput's hints (Apple USB 104×75, default 69×55); `sizeSource` says which.
`preset_scale()` is screen px per pad mm over `PRESET_PX_PER_MM` (the 124 mm /
1920 px anchor, a choice), clamped 0.5–2; `preset_gains()` and `Curve.presetFor`
must agree (a node cross-check test enforces it). Sessions carry `device`,
per-pad histograms live in `pad_minutes`, optimize log rows carry `device`, and
`device_log()` treats rows without one as every pad's. `settle()` always writes
the full log.

## Report, hand, mouse, auto-off

`report` builds the week from the `days` table (which also keeps `mouse`
seconds and an `apps` JSON per day) plus `today.json` and the optimize log.
`hand_verdict` is pure: bottom-row heat mass left vs right, and the palm
centroid, vote. `MouseWatch` counts cursor motion with no finger on the pad
as a mouse; it polls the Hyprland socket at 5 Hz, 20 Hz while moving.
Auto-off is opt-in via the `auto-off` marker file in the state dir: after
`AUTO_OFF_AFTER` seconds of mouse streak the recorder runs
`trackpads.py set <device> enabled false`, and a tap or a `DELIBERATE_MM` move
seen on the still-reporting evdev node runs `enabled true`. Turning the
setting off restores the pad within a tick. Never make this default-on.

## Stray touches and the put-back guard

`stray_kind()` is pure and names a finished one-finger move as a brush or a
rest; it needs `cursor` (pixels the cursor moved during the touch, from the
last cursor poll before the touch to a poll at its end), `gap`, `x0`/`y0`
(normalised start) and `clicked`, all now on the session record and in the
`sessions` table. The guard is opt-in via the `stray-guard` marker file:
`judge_strays()` queues one put-back, `stray_guard()` decides it with the pure
`guard_verdict()` (wait STRAY_HOLD, cancel on a finger or a mouse drift) and
warps through `hyprctl dispatch 'hl.dsp.cursor.move({ x, y })'`. `is_regret()`
counts a real move right after a put-back. The pad on gus reports neither
pressure nor contact size, so libinput's palm thresholds are not a lever here.

## Gestures

Hyprland's own: `hl.gesture({ fingers, direction, action })` with directions
left, right, up, down, pinchin, pinchout (and horizontal/vertical for 1:1 pair
actions) and native actions workspace, special, fullscreen, close, float, move,
resize, scroll_move, cursor_zoom; anything else is a Lua lambda calling
`hl.exec_cmd` or `hl.dispatch`. The recorder validates slot and action ids,
writes `~/.local/state/omarchy/toggles/hypr/zz-trackpad-pulse-gestures.lua`
through Trackpad Plus's `atomic_write`, and runs `hyprctl reload config-only`.
Add an action to `CATALOGUE` only; never accept free text from the panel.

## The optimizer's contract

Log rows in `optimize-log.json`: `changes` (each with `key`, `from`, `to`,
`direction`, `watch`), `evidence` (the rates before), `watch` (what the next
pass reads), then once judged `judgement` (`kept`, `undo`, `undone`,
`kept by you`, `overridden`), `judgeReason`, `judgedTs`, `after`. An undo row
has `undo`, `reverts` and `hold` (`key`, `direction`, `moves`); the same
change is not re-proposed until that many moves since the undo ask for it.
`JUDGE_MOVES` / `JUDGE_SECONDS` gate the verdict, `HELPED_BY` is what a nudge
must earn, `WORSE_BY` is what a shape change may not lose.
