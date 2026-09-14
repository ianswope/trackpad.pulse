#!/usr/bin/env python3
"""Install Trackpad Pulse: the panel, Trackpad Plus's backend, and the recorder service.

The shape is the one the other Pulse plugins settled on: validate first, keep
a rollback copy, publish atomically, start the user service, and place the
widget through the running shell rather than by editing its config underneath
it. Trackpad Plus and the widgets it replaced are disabled, not deleted, so
going back is one enable away and their settings files are shared anyway.

    python3 install.py

`omarchy plugin add https://github.com/nixfred/trackpad.pulse.git --enable`
works too; the panel then offers to start the recorder from its Overview.
"""
from pathlib import Path
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time

PLUGIN_ID = 'nixfred.trackpad-pulse'
UNITS = ('trackpad-pulse.service',)
PAYLOAD = ('manifest.json', 'Panel.qml', 'CurveEditor.qml', 'Curve.js', 'Model.js', 'Pulse.js', 'TrackpadChip.qml',
           'TouchHistoryGraph.qml', 'SpeedHistogram.qml', 'trackpads.py', 'touchpad-state', 'touchpad-sensitivity',
           'README.md', 'LICENSE', 'collectors')
PLACEMENT_TRIES = 6
# The widgets this one replaces. They all write the same settings files, so
# two of them enabled at once would fight over one Lua rule.
SUPERSEDED = ('davefano.trackpad-plus', 'awkent01.touchpad', 'local.touchpads')


def atomic_write(path, payload, mode):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def entry_id(entry):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get('id')
    return None


def update_layout(raw):
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise ValueError('shell.json must contain an object at the top level.')
    bar = data.setdefault('bar', {})
    if not isinstance(bar, dict):
        raise ValueError('shell.json must contain an object at bar.')
    layout = bar.setdefault('layout', {})
    if not isinstance(layout, dict):
        raise ValueError('shell.json must contain an object at bar.layout.')
    for section in ('left', 'center', 'right'):
        entries = layout.setdefault(section, [])
        if not isinstance(entries, list):
            raise ValueError('bar.layout.' + section + ' must be an array.')
    found = False
    for section in ('left', 'center', 'right'):
        entries = []
        for entry in layout[section]:
            if entry_id(entry) == PLUGIN_ID:
                if found:
                    continue
                found = True
            entries.append(entry)
        layout[section] = entries
    if not found:
        layout['right'].append({'id': PLUGIN_ID})
    return (json.dumps(data, indent=2) + '\n').encode('utf-8')


def symlinked_ancestors(*paths):
    home = Path.home()
    found = set()
    for path in paths:
        for candidate in (path, *path.parents):
            if candidate == home:
                break
            if candidate.is_symlink():
                found.add(str(candidate))
    return sorted(found)


def place_through_shell():
    for _ in range(PLACEMENT_TRIES):
        try:
            result = subprocess.run(['omarchy-shell', 'shell', 'putBarWidget', PLUGIN_ID, '{"section": "right"}'],
                                    capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        answer = (getattr(result, 'stdout', '') or '').strip()
        if result.returncode == 0 and answer == 'ok':
            return True
        if answer != 'not ready':
            return False
        time.sleep(0.5)
    return False


def retire_superseded():
    for plugin in SUPERSEDED:
        subprocess.run(['omarchy-shell', '-q', 'shell', 'setPluginEnabled', plugin, 'false'],
                       capture_output=True, timeout=10, check=False)


def stage(source, staging):
    for name in PAYLOAD:
        path = source / name
        if not path.exists():
            raise RuntimeError('Missing install payload: ' + name)
        if path.is_dir():
            shutil.copytree(path, staging / name, ignore=shutil.ignore_patterns('.*', '__pycache__', '*.part'))
        else:
            shutil.copy2(path, staging / name)


def main():
    source = Path(__file__).resolve().parent
    home = Path.home()
    config = home / '.config/omarchy/shell.json'
    dest = config.parent / 'plugins' / PLUGIN_ID
    units = home / '.config/systemd/user'
    linked = symlinked_ancestors(config, dest, units)
    if linked:
        raise RuntimeError('Resolve symlinked install destinations explicitly before installing: ' + ', '.join(linked))
    config.parent.mkdir(parents=True, exist_ok=True)
    raw = config.read_bytes() if config.exists() else b'{}'
    update_layout(raw)
    config_mode = stat.S_IMODE(config.stat().st_mode) & 0o777 if config.exists() else 0o644

    backups = home / '.local/state/omarchy/backups'
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = Path(tempfile.mkdtemp(prefix='trackpad-pulse-' + stamp + '-', dir=backups))
    if config.exists():
        shutil.copy2(config, backup / 'shell.json')
    if dest.exists():
        shutil.copytree(dest, backup / 'plugin', symlinks=True)
    for unit in UNITS:
        if (units / unit).exists():
            shutil.copy2(units / unit, backup / unit)

    staging = dest.parent / ('.' + PLUGIN_ID + '.incoming')
    retired = dest.parent / ('.' + PLUGIN_ID + '.previous')
    shutil.rmtree(staging, ignore_errors=True)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        stage(source, staging)
        shutil.rmtree(retired, ignore_errors=True)
        if dest.exists():
            dest.rename(retired)
        staging.rename(dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        print('Publication failed; the previous release is still in place. Rollback copies: ' + str(backup))
        raise

    def unpublish():
        if not retired.exists():
            return
        shutil.rmtree(dest, ignore_errors=True)
        retired.rename(dest)
        for unit in UNITS:
            saved = backup / unit
            if saved.is_file():
                shutil.copy2(saved, units / unit)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, timeout=30, check=False)
        for unit in UNITS:
            subprocess.run(['systemctl', '--user', 'restart', unit], capture_output=True, timeout=30, check=False)

    try:
        units.mkdir(parents=True, exist_ok=True)
        for unit in UNITS:
            payload = dest / 'collectors' / unit
            text = payload.read_text()
            text = re.sub(r'ExecStart=\S+ \S*%h/\.config/omarchy/plugins/\S+?/trackpad_pulse\.py',
                          'ExecStart=/usr/bin/python3 %h/.config/omarchy/plugins/' + PLUGIN_ID + '/collectors/trackpad_pulse.py', text)
            if (PLUGIN_ID + '/collectors/trackpad_pulse.py') not in text:
                raise RuntimeError('Could not point ' + unit + ' at this plugin\'s collector.')
            atomic_write(units / unit, text.encode('utf-8'), stat.S_IMODE(payload.stat().st_mode) & 0o777)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, timeout=30)
        for unit in UNITS:
            subprocess.run(['systemctl', '--user', 'enable', unit], check=True, timeout=30)
            subprocess.run(['systemctl', '--user', 'restart', unit], check=True, timeout=30)
        subprocess.run(['omarchy-shell', 'shell', 'rescanPlugins'], check=True, timeout=30)
        retire_superseded()
        if not place_through_shell():
            atomic_write(config, update_layout(config.read_bytes()), config_mode)
    except Exception:
        unpublish()
        print('Install did not complete; the previous release was put back. Rollback copies: ' + str(backup))
        raise
    shutil.rmtree(retired, ignore_errors=True)
    print('Installed Trackpad Pulse. Backup: ' + str(backup))


if __name__ == '__main__':
    main()
