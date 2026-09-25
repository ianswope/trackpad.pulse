# Developing Trackpad Pulse

Clone and work outside the installed plugin:

```sh
git clone https://github.com/nixfred/trackpad.pulse.git
cd trackpad.pulse
```

`main` is the release line. Preserve Trackpad Plus's and omarchy-touchpad-widget's history and MIT notices. Do not commit runtime settings, private configuration, caches or backups.

## Versions

Semver, in `manifest.json` only. The panel header, the About page and `status` read it; the README badge reads it from GitHub. A fix bumps the patch, a new capability the minor. Add a `CHANGELOG.md` entry and tag `vX.Y.Z` on the release commit. Docs-only commits need no bump.

## Architecture

Two halves that never touch each other's files:

- **Settings** are Trackpad Plus: `trackpads.py` (device discovery, validation, file locking, journalled persistence, per-device `hl.device` updates, libinput-validated curves), `Model.js`, `Curve.js`, `CurveEditor.qml`, and the legacy `touchpad-state` / `touchpad-sensitivity` helpers. `Panel.qml` keeps Trackpad Plus's action queue, debouncing, stale-read rejection and process deadlines verbatim; `test-selection.js` executes those functions out of the file, so keep them at two-space indentation at the root of `Panel.qml`.
- **Telemetry** is the recorder in `collectors/trackpad_pulse.py`. `Pad` is pure: feed it events, read counters and session records; `flag_session` names overshoot corrections and re-strokes against the session before. `propose` is pure too: current settings, sessions and the speed histogram in, a proposal with reasons out. `Recorder` owns the files. The panel reads `snapshot.json`, `history.json` and the tmpfs `live.json` through `FileView` and never writes them. `Pulse.js` formats and derives readouts; `TrackpadChip.qml`, `TouchHistoryGraph.qml` and `SpeedHistogram.qml` draw.

Panel rendering rules learned the hard way: no `shadowBlur` on any Canvas (it rasterises on the GUI thread and froze the bar), every chip coalesces paints onto one timer at 20 fps or less, and the live file is 20 Hz. `KeyboardPanel` is handed the content's real height and nothing scrolls.

## Tests

Run on an Omarchy host with Python 3, Node.js, Quickshell, Qt 6 Quick Controls/Test and the Qt tools (`qmllint`, `qmltestrunner`):

```sh
python3 test_collector.py      # classifier, discovery, history buckets, unit text
python3 test_trackpads.py      # Trackpad Plus backend
node test-selection.js         # Panel.qml queue logic, Pulse.js readouts
git add -A && python3 test_install.py   # tracked-only install against a fake compositor
python3 test_ipc.py            # the panel's own IpcHandler in an offscreen shell
python3 lint-qml.py            # qmllint over every QML file
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QT_QUICK_CONTROLS_STYLE=Basic \
  /usr/lib/qt6/bin/qmltestrunner -input tst_curve.qml
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QT_QUICK_CONTROLS_STYLE=Basic \
  /usr/lib/qt6/bin/qmltestrunner -input tst_chip.qml   # the chip fades a lift with the animation off
perl -c touchpad-state
bash -n touchpad-sensitivity
git diff --check
```

`test_install.py` copies git-tracked files, so stage new files before running it. `lint-qml.py` fails on every warning except the identified host-metadata gaps for `Panel.qml`. New QML files must lint clean without exceptions.

## Live checks before a release

1. Run the suite.
2. `python3 install.py` on a machine with a trackpad, then `omarchy restart shell` (the shell's hot reload leaves a stale IPC handler behind; a restart is the honest check).
3. Open every page and read the bottom edge: `omarchy-shell nixfred.trackpad-pulse status` reports `contentNeeded` against `availableHeight`; the first must fit inside the second on the shortest screen you support.
4. Touch the pad with the Overview open and watch the chip, the finger table and the verdict; probe GUI-thread stalls with a loop of `omarchy-shell shell ping` while it is open.
5. Grant and revoke access from the Touch lab; check `getfacl /dev/input/eventN` both ways.
6. Screenshot every page and look at every image before it is committed.
