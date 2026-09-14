#!/usr/bin/env python3
"""Trackpad Pulse: touchpad telemetry recorder, seven days of history, no root.

Reads the touchpad's own event node when the user may open it, falls back to
the compositor's cursor when they may not, and records what the fingers did:
touches, taps, clicks, scrolls, swipes, pinches, rejected palms, distance,
speed, and the time-weighted distribution of finger speed that the pointer-
feel editor draws under its acceleration curve.

Settings never come through here. They are David Fano's trackpads.py, which
this plugin ships unchanged.

Files
  $XDG_STATE_HOME/trackpad-pulse/snapshot.json   counters, device facts, access
  $XDG_STATE_HOME/trackpad-pulse/history.json    1h / 24h / 7d buckets
  $XDG_STATE_HOME/trackpad-pulse/history.sqlite3 one row per minute, 7 days
  $XDG_STATE_HOME/trackpad-pulse/today.json      today's counters, survives restarts
  $XDG_RUNTIME_DIR/trackpad-pulse/live.json      finger positions, tmpfs, 20 Hz
"""
import argparse
import array
import fcntl
import glob
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import socket
import sqlite3
import struct
import subprocess
import time
from collections import deque

STATE = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'trackpad-pulse'
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR') or ('/run/user/' + str(os.getuid()))) / 'trackpad-pulse'
LINKS = {'repo': 'https://github.com/nixfred/trackpad.pulse',
         'author': 'https://nixfred.com',
         'plugins': 'https://omarchy.nixfred.com',
         'upstream': 'https://github.com/davefano/omarchy-trackpad-plus',
         'origin': 'https://github.com/awkent01/omarchy-touchpad-widget'}
UNIT_NAME = 'trackpad-pulse.service'

# Omarchy removes users from the `input` group on purpose (migration
# 1787865477: membership lets any process keylog). This rule grants the
# logged-in seat read access to touchpad nodes only, through logind's uaccess
# ACLs, and never touches a keyboard node. It is the one thing here that needs
# root, once, and it is offered, never required.
UDEV_RULE_PATH = Path('/etc/udev/rules.d/70-trackpad-pulse.rules')
UDEV_RULE = ('# Trackpad Pulse: let the active seat read its own touchpad. Keyboards stay root:input.\n'
             'SUBSYSTEM=="input", ENV{ID_INPUT_TOUCHPAD}=="1", TAG+="uaccess"\n')

# ---- evdev ----------------------------------------------------------------
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR = 0x2f, 0x30
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TOOL_TYPE, ABS_MT_TRACKING_ID, ABS_MT_PRESSURE = 0x37, 0x39, 0x3a
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BTN_TOOL_FINGER, BTN_TOUCH = 0x145, 0x14a
BTN_TOOL_DOUBLETAP, BTN_TOOL_TRIPLETAP, BTN_TOOL_QUADTAP, BTN_TOOL_QUINTTAP = 0x14d, 0x14e, 0x14f, 0x148
MT_TOOL_PALM = 2
INPUT_PROP_POINTER, INPUT_PROP_DIRECT = 0, 1
EVENT = struct.Struct('@qqHHi')
CLOCK_MONOTONIC = 1


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord('E') << 8) | nr


def EVIOCGNAME(n): return _ioc(2, 0x06, n)
def EVIOCGID(): return _ioc(2, 0x02, 8)
def EVIOCGABS(code): return _ioc(2, 0x40 + code, 24)
def EVIOCSCLOCKID(): return _ioc(1, 0xa0, 4)


# The speed histogram: how much time the fingers spent at each speed, in
# 5 mm/s bins up to 150 mm/s and one bin for everything faster. libinput's
# custom curve runs 0..4.2 device units per millisecond, and touchpad deltas
# are normalized to 1000 dpi, so one unit per ms is 25.4 mm/s: the whole
# curve editor spans the first 21 bins. Everything past that lands on the
# curve's flat tail, which is exactly what the last bin says.
BIN_MM_S = 5.0
BINS = 30
MM_PER_UNIT_MS = 25.4
REST_MM_S = 2.0          # slower than this is a finger resting, not moving
TAP_SECONDS = 0.25
TAP_MM = 3.0
SWIPE_MM = 8.0
PINCH_MM = 6.0
LIVE_INTERVAL = 1 / 20
RETENTION = 7 * 86400

COUNTERS = ('touches', 'taps', 'taps2', 'taps3', 'clicks', 'rightClicks', 'moves', 'scrolls', 'pinches',
            'swipes3', 'swipes4', 'palms', 'distance', 'scroll', 'active', 'moving', 'frames')


def zero_counters():
    return {k: 0 for k in COUNTERS}


def read(path):
    try:
        return Path(path).read_text(errors='replace')
    except (OSError, ValueError):
        return ''


def atomic(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / name
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, separators=(',', ':'), ensure_ascii=True))
    tmp.replace(path)


def bit(words, index):
    """Test one bit of a /proc/bus/input/devices bitmap ("e520 10000 0 0")."""
    parts = words.split()
    parts.reverse()
    word, offset = divmod(index, 64)
    if word >= len(parts):
        return False
    try:
        return (int(parts[word], 16) >> offset) & 1 == 1
    except ValueError:
        return False


def parse_devices(text):
    """Touchpads from /proc/bus/input/devices: multitouch, indirect, with an event node.

    Readable by everyone, so discovery always works even when the node does
    not open; the panel can then say which pad it cannot read and why.
    """
    pads = []
    for block in text.split('\n\n'):
        fields = {}
        for line in block.splitlines():
            if len(line) > 3 and line[1:3] == ': ':
                key, _, value = line[3:].partition('=')
                fields[line[0] + ':' + key] = value
        name = fields.get('N:Name', '').strip('"')
        handlers = fields.get('H:Handlers', '').split()
        node = next((h for h in handlers if h.startswith('event')), None)
        abs_bits = fields.get('B:ABS', '')
        if not node or not abs_bits:
            continue
        multitouch = bit(abs_bits, ABS_MT_SLOT) and bit(abs_bits, ABS_MT_POSITION_X)
        single = bit(abs_bits, ABS_X) and bit(abs_bits, ABS_Y) and bit(fields.get('B:KEY', ''), BTN_TOOL_FINGER)
        pointer = bit(fields.get('B:PROP', ''), INPUT_PROP_POINTER)
        direct = bit(fields.get('B:PROP', ''), INPUT_PROP_DIRECT)
        named = re.search(r'touchpad|trackpad', name, re.I) is not None
        if direct or not (multitouch or single) or not (pointer or named):
            continue
        pads.append({'node': '/dev/input/' + node, 'name': name[:128], 'phys': fields.get('P:Phys', ''),
                     'sysfs': fields.get('S:Sysfs', ''), 'multitouch': multitouch,
                     'pressure': bit(abs_bits, ABS_MT_PRESSURE), 'major': bit(abs_bits, ABS_MT_TOUCH_MAJOR)})
    return pads


def abs_info(fd, code):
    buf = array.array('i', [0] * 6)
    try:
        fcntl.ioctl(fd, EVIOCGABS(code), buf)
    except OSError:
        return None
    value, minimum, maximum, fuzz, flat, resolution = buf
    if maximum <= minimum:
        return None
    return {'min': minimum, 'max': maximum, 'res': resolution}


def identity(fd):
    buf = array.array('H', [0] * 4)
    try:
        fcntl.ioctl(fd, EVIOCGID(), buf)
    except OSError:
        return {}
    bus = {0x03: 'usb', 0x05: 'bluetooth', 0x11: 'ps/2', 0x18: 'i2c', 0x1d: 'rmi', 0x19: 'host', 0x06: 'virtual'}.get(buf[0], hex(buf[0]))
    return {'bus': bus, 'vendor': '%04x' % buf[1], 'product': '%04x' % buf[2], 'version': buf[3]}


class Pad:
    """One touchpad's event stream turned into counts. Pure: feed() and tick() only.

    Positions arrive in device units and leave in millimetres via the axis
    resolution the kernel reports, so distance and speed are physical whether
    the pad is a 4010-unit ELAN or a 7000-unit Apple.
    """

    def __init__(self, info, axes, now):
        self.info = info
        self.axes = axes
        x, y = axes.get('x') or {'min': 0, 'max': 1, 'res': 0}, axes.get('y') or {'min': 0, 'max': 1, 'res': 0}
        self.res_x = x['res'] or 30.0
        self.res_y = y['res'] or 30.0
        self.range_x = (x['min'], max(x['max'], x['min'] + 1))
        self.range_y = (y['min'], max(y['max'], y['min'] + 1))
        self.width_mm = (self.range_x[1] - self.range_x[0]) / self.res_x
        self.height_mm = (self.range_y[1] - self.range_y[0]) / self.res_y
        self.multitouch = info.get('multitouch', True)
        self.pressure_axis = axes.get('pressure')
        self.slots = {}
        self.slot = 0
        self.pending = {}
        self.buttons = {}
        self.touch = False
        self.session = None
        self.counts = zero_counters()
        self.hist = [0.0] * (BINS + 1)
        self.peak = 0.0
        self.peak_at = 0.0
        self.last_frame = now
        self.last_touch = 0.0
        self.frames = deque()
        self.hz = 0.0
        self.hz_seen = 0.0
        self.live = []
        self.speed = 0.0
        self.fingers = 0
        self.dirty = False

    # -- events ------------------------------------------------------------
    def feed(self, typ, code, value, now):
        if typ == EV_ABS:
            if code == ABS_MT_SLOT:
                self.slot = value
            elif code == ABS_MT_TRACKING_ID:
                self.pending.setdefault(self.slot, {})['id'] = value
            elif code == ABS_MT_POSITION_X or (code == ABS_X and not self.multitouch):
                self.pending.setdefault(self.slot, {})['x'] = value
            elif code == ABS_MT_POSITION_Y or (code == ABS_Y and not self.multitouch):
                self.pending.setdefault(self.slot, {})['y'] = value
            elif code == ABS_MT_PRESSURE:
                self.pending.setdefault(self.slot, {})['p'] = value
            elif code == ABS_MT_TOUCH_MAJOR:
                self.pending.setdefault(self.slot, {})['major'] = value
            elif code == ABS_MT_TOOL_TYPE:
                self.pending.setdefault(self.slot, {})['tool'] = value
        elif typ == EV_KEY:
            if code == BTN_TOUCH:
                self.touch = value == 1
                if not self.multitouch:
                    self.pending.setdefault(0, {})['id'] = 0 if value else -1
            elif code in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                pressed = value == 1 and not self.buttons.get(code)
                self.buttons[code] = value == 1
                if pressed:
                    self.counts['clicks' if code == BTN_LEFT else 'rightClicks'] += 1
                    if self.session:
                        self.session['clicked'] = True
                    self.dirty = True
        elif typ == EV_SYN and code == SYN_REPORT:
            self._frame(now)

    def _frame(self, now):
        dt = max(0.0, now - self.last_frame)
        self.last_frame = now
        for slot, change in self.pending.items():
            state = self.slots.get(slot)
            if 'id' in change:
                if change['id'] < 0:
                    if state:
                        state['id'] = -1
                    continue
                if not state or state['id'] != change['id']:
                    state = {'id': change['id'], 'x': None, 'y': None, 'p': 0, 'major': 0, 'tool': 0, 'px': None, 'py': None, 'moved': 0.0}
                    self.slots[slot] = state
            if state is None:
                continue
            for key in ('x', 'y', 'p', 'major', 'tool'):
                if key in change:
                    state[key] = change[key]
        self.pending = {}
        active = [s for s in self.slots.values() if s['id'] >= 0 and s['x'] is not None and s['y'] is not None]
        fingers = len(active)
        # Motion since the last frame, per finger, in millimetres.
        speeds, dists = [], []
        for s in active:
            if s['px'] is not None and dt > 0:
                d = math.hypot((s['x'] - s['px']) / self.res_x, (s['y'] - s['py']) / self.res_y)
                s['moved'] += d
                dists.append(d)
                speeds.append(d / dt)
            s['px'], s['py'] = s['x'], s['y']
        speed = sum(speeds) / len(speeds) if speeds else 0.0
        dist = sum(dists) / len(dists) if dists else 0.0
        self.speed = speed
        self.fingers = fingers
        if fingers:
            # Wall-clock, unlike the event timestamps, because the panel compares
            # it with the time of day to say how long ago the last touch was.
            self.last_touch = time.time()
            self.counts['frames'] += 1
            self.frames.append(now)
            while self.frames and now - self.frames[0] > 1.0:
                self.frames.popleft()
            self.hz = len(self.frames) if now - (self.frames[0] if self.frames else now) >= 0.5 else self.hz
            if self.hz:
                self.hz_seen = self.hz
            if self.session is None:
                self.session = {'start': now, 'max': 0, 'dist': 0.0, 'palm': False, 'clicked': False, 'gap0': None, 'gapMax': 0.0}
                self.counts['touches'] += 1
            ses = self.session
            ses['max'] = max(ses['max'], fingers)
            ses['dist'] += dist
            if any(s['tool'] == MT_TOOL_PALM for s in active):
                ses['palm'] = True
            if fingers == 2:
                a, b = active[0], active[1]
                gap = math.hypot((a['x'] - b['x']) / self.res_x, (a['y'] - b['y']) / self.res_y)
                if ses['gap0'] is None:
                    ses['gap0'] = gap
                ses['gapMax'] = max(ses['gapMax'], abs(gap - ses['gap0']))
                if dist > 0:
                    self.counts['scroll'] += dist
            if dist > 0:
                self.counts['distance'] += dist
            if dt > 0:
                self.counts['active'] += dt
                if speed >= REST_MM_S:
                    self.counts['moving'] += dt
                    index = min(BINS, int(speed / BIN_MM_S))
                    self.hist[index] += dt
                    if speed > self.peak:
                        self.peak, self.peak_at = speed, time.time()
            self.dirty = True
        elif self.session is not None:
            self._end_session(now)
        if fingers or self.live:
            self.live = [{'slot': slot, 'x': round((s['x'] - self.range_x[0]) / (self.range_x[1] - self.range_x[0]), 4),
                          'y': round((s['y'] - self.range_y[0]) / (self.range_y[1] - self.range_y[0]), 4),
                          'p': self._pressure(s), 'palm': s['tool'] == MT_TOOL_PALM,
                          'speed': round(speeds[i] if i < len(speeds) else 0.0, 1)}
                         for i, (slot, s) in enumerate((k, v) for k, v in self.slots.items() if v['id'] >= 0 and v['x'] is not None and v['y'] is not None)]
            self.dirty = True
        for slot in [k for k, v in self.slots.items() if v['id'] < 0]:
            del self.slots[slot]

    def _pressure(self, s):
        if self.pressure_axis and s['p']:
            span = max(1, self.pressure_axis['max'] - self.pressure_axis['min'])
            return round(min(1.0, max(0.0, (s['p'] - self.pressure_axis['min']) / span)), 3)
        return None

    def _end_session(self, now):
        ses, self.session = self.session, None
        kind = classify(ses, now)
        self.counts[{'tap': 'taps', 'tap2': 'taps2', 'tap3': 'taps3', 'move': 'moves', 'scroll': 'scrolls', 'pinch': 'pinches',
                     'swipe3': 'swipes3', 'swipe4': 'swipes4', 'palm': 'palms'}.get(kind, 'moves')] += 1
        self.dirty = True

    def tick(self, now):
        """Called with no event for a while: lets a stuck session close and hz decay."""
        if self.frames and now - self.frames[-1] > 1.0:
            self.frames.clear()
            self.hz = 0.0
        if self.session is not None and not any(s['id'] >= 0 for s in self.slots.values()) and now - self.last_frame > 0.5:
            self._end_session(now)
            self.live = []
            self.fingers = 0
            self.speed = 0.0
            self.dirty = True

    def take(self):
        """Hand over the counters accumulated since the last take and reset them."""
        counts, self.counts = self.counts, zero_counters()
        hist, self.hist = self.hist, [0.0] * (BINS + 1)
        return counts, hist

    def facts(self):
        f = dict(self.info)
        f.update({'width': round(self.width_mm, 1), 'height': round(self.height_mm, 1),
                  'resX': self.res_x, 'resY': self.res_y, 'unitsX': self.range_x[1] - self.range_x[0], 'unitsY': self.range_y[1] - self.range_y[0],
                  'slots': (self.axes.get('slot') or {}).get('max', 0) + 1 if self.axes.get('slot') else 1,
                  'pressure': bool(self.pressure_axis), 'major': bool(self.axes.get('major')),
                  'hz': round(self.hz_seen), 'hzNow': round(self.hz), 'fingers': self.fingers, 'speed': round(self.speed, 1), 'lastTouch': self.last_touch})
        return f


def classify(ses, now):
    """Name a touch session from how many fingers, how far, how long and whether it clicked."""
    duration = now - ses['start']
    if ses['palm']:
        return 'palm'
    if duration <= TAP_SECONDS and ses['dist'] < TAP_MM and not ses['clicked']:
        return {1: 'tap', 2: 'tap2'}.get(ses['max'], 'tap3')
    if ses['max'] >= 4:
        return 'swipe4' if ses['dist'] >= SWIPE_MM else 'move'
    if ses['max'] == 3:
        return 'swipe3' if ses['dist'] >= SWIPE_MM else 'move'
    if ses['max'] == 2:
        if ses['gapMax'] >= PINCH_MM:
            return 'pinch'
        return 'scroll' if ses['dist'] >= TAP_MM else 'move'
    return 'move'


# ---- access ---------------------------------------------------------------
def in_input_group():
    try:
        import grp
        return any(grp.getgrgid(g).gr_name == 'input' for g in os.getgroups())
    except (KeyError, OSError, ImportError):
        return False


def udev_rule_present():
    try:
        return UDEV_RULE_PATH.read_text() == UDEV_RULE
    except OSError:
        return False


def open_pad(info, now):
    fd = os.open(info['node'], os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, EVIOCSCLOCKID(), array.array('i', [CLOCK_MONOTONIC]))
    except OSError:
        pass
    axes = {'x': abs_info(fd, ABS_MT_POSITION_X) or abs_info(fd, ABS_X),
            'y': abs_info(fd, ABS_MT_POSITION_Y) or abs_info(fd, ABS_Y),
            'slot': abs_info(fd, ABS_MT_SLOT), 'pressure': abs_info(fd, ABS_MT_PRESSURE), 'major': abs_info(fd, ABS_MT_TOUCH_MAJOR)}
    if not axes['x'] or not axes['y']:
        os.close(fd)
        raise OSError('no position axes on ' + info['node'])
    name = array.array('B', [0] * 256)
    try:
        fcntl.ioctl(fd, EVIOCGNAME(256), name)
        info = dict(info, kernelName=name.tobytes().split(b'\0')[0].decode(errors='replace')[:128])
    except OSError:
        pass
    info.update(identity(fd))
    info['multitouch'] = bool(axes['slot'])
    return fd, Pad(info, axes, now)


# ---- cursor fallback ------------------------------------------------------
class Cursor:
    """What the compositor will say about the pointer when the pad itself is closed to us.

    Distance and speed are in logical pixels, not millimetres, and there are
    no fingers, taps or gestures in it: that is the whole difference, and the
    panel says so.
    """

    def __init__(self):
        self.path = None
        self.x = self.y = None
        self.at = None
        self.counts = {'distance': 0.0, 'active': 0.0, 'moving': 0.0}
        self.hist = [0.0] * (BINS + 1)
        self.peak = 0.0
        self.peak_at = 0.0
        self.speed = 0.0
        self.last_move = 0.0

    def socket_path(self):
        runtime = os.environ.get('XDG_RUNTIME_DIR') or '/run/user/' + str(os.getuid())
        sig = os.environ.get('HYPRLAND_INSTANCE_SIGNATURE')
        candidates = [os.path.join(runtime, 'hypr', sig, '.socket.sock')] if sig else []
        candidates += sorted(glob.glob(os.path.join(runtime, 'hypr', '*', '.socket.sock')), key=os.path.getmtime, reverse=True)
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def poll(self, now):
        if not self.path or not os.path.exists(self.path):
            self.path = self.socket_path()
            if not self.path:
                return False
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(0.5)
                s.connect(self.path)
                s.sendall(b'cursorpos')
                reply = s.recv(64).decode(errors='replace')
            x, y = (float(v) for v in reply.split(',')[:2])
        except (OSError, ValueError):
            self.path = None
            return False
        if self.x is not None and self.at is not None:
            dt = now - self.at
            d = math.hypot(x - self.x, y - self.y)
            if d > 0 and dt > 0:
                speed = d / dt
                self.counts['distance'] += d
                self.counts['moving'] += dt
                self.counts['active'] += dt
                # Pixel speed lands in the same bins scaled by 10, so 5 mm/s
                # bins read as 50 px/s bins on the cursor-only histogram.
                self.hist[min(BINS, int(speed / (BIN_MM_S * 10)))] += dt
                self.speed = speed
                self.last_move = now
                if speed > self.peak:
                    self.peak, self.peak_at = speed, time.time()
            else:
                self.speed = 0.0
        self.x, self.y, self.at = x, y, now
        return True

    def take(self):
        counts, self.counts = self.counts, {'distance': 0.0, 'active': 0.0, 'moving': 0.0}
        hist, self.hist = self.hist, [0.0] * (BINS + 1)
        return counts, hist


# ---- persistence ----------------------------------------------------------
def db_open():
    db = sqlite3.connect(STATE / 'history.sqlite3', timeout=5)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS minutes (ts REAL PRIMARY KEY, touches INTEGER, taps INTEGER, clicks INTEGER, '
               'distance REAL, scroll REAL, swipes INTEGER, palms INTEGER, active REAL, peak REAL, hist TEXT, source TEXT, boot TEXT)')
    return db


def record(db, ts, counts, hist, source):
    db.execute('INSERT OR REPLACE INTO minutes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (ts, counts.get('touches', 0), counts.get('taps', 0) + counts.get('taps2', 0) + counts.get('taps3', 0),
                counts.get('clicks', 0) + counts.get('rightClicks', 0), counts.get('distance', 0.0), counts.get('scroll', 0.0),
                counts.get('swipes3', 0) + counts.get('swipes4', 0) + counts.get('pinches', 0), counts.get('palms', 0),
                counts.get('active', 0.0), counts.get('peak', 0.0), json.dumps([round(v, 3) for v in hist]), source,
                read('/proc/sys/kernel/random/boot_id').strip()))
    db.execute('DELETE FROM minutes WHERE ts < ?', (ts - RETENTION,))
    db.commit()


def history(db, seconds, now=None):
    """Buckets for the graph: [start, touches, distance mm, peak mm/s, active s, taps, boot]."""
    now = time.time() if now is None else now
    bucket = max(60, seconds / 240)
    rows = db.execute('SELECT MIN(ts), SUM(touches), SUM(distance), MAX(peak), SUM(active), SUM(taps), boot, COUNT(*) FROM minutes '
                      'WHERE ts>=? AND ts<=? GROUP BY CAST(ts/? AS INTEGER), boot ORDER BY MIN(ts)', (now - seconds, now, bucket)).fetchall()
    points = [[r[0], r[1], round(r[2], 1), round(r[3], 1), round(r[4], 1), r[5], r[6]] for r in rows]
    return {'seconds': seconds, 'bucket': bucket, 'now': now, 'points': points, 'count': sum(r[7] for r in rows),
            'peak': max((r[3] for r in rows), default=0), 'touches': sum(r[1] for r in rows),
            'distance': round(sum(r[2] for r in rows), 1), 'busiest': max((r[1] for r in rows), default=0)}


def week_summary(db, now):
    row = db.execute('SELECT SUM(touches), SUM(taps), SUM(clicks), SUM(distance), SUM(scroll), SUM(swipes), SUM(palms), SUM(active), MAX(peak), COUNT(*) '
                     'FROM minutes WHERE ts >= ?', (now - RETENTION,)).fetchone()
    hist = [0.0] * (BINS + 1)
    for (raw,) in db.execute('SELECT hist FROM minutes WHERE ts >= ?', (now - RETENTION,)):
        try:
            for i, v in enumerate(json.loads(raw)[:BINS + 1]):
                hist[i] += v
        except (ValueError, TypeError):
            pass
    keys = ('touches', 'taps', 'clicks', 'distance', 'scroll', 'swipes', 'palms', 'active', 'peak', 'minutes')
    return dict({k: (row[i] or 0) for i, k in enumerate(keys)}, hist=[round(v, 2) for v in hist])


def day_key(ts):
    return time.strftime('%Y-%m-%d', time.localtime(ts))


class Recorder:
    def __init__(self):
        self.pads = {}          # node -> (fd, Pad)
        self.known = {}         # node -> info from /proc even when unopenable
        self.denied = {}        # node -> error text
        self.cursor = Cursor()
        self.db = db_open()
        self.today = self._load_today()
        self.minute = {'counts': zero_counters(), 'hist': [0.0] * (BINS + 1), 'peak': 0.0, 'start': time.time()}
        self.last_scan = 0.0
        self.last_live = 0.0
        self.last_snapshot = 0.0
        self.last_history = 0.0
        self.live_clear_pending = False

    def _load_today(self):
        try:
            saved = json.loads((STATE / 'today.json').read_text())
            if saved.get('day') == day_key(time.time()):
                return saved
        except (OSError, ValueError):
            pass
        return self._fresh_today(time.time())

    def _fresh_today(self, ts):
        return {'day': day_key(ts), 'counts': zero_counters(), 'hist': [0.0] * (BINS + 1), 'peak': 0.0, 'peakAt': 0.0, 'lastTouch': 0.0,
                'cursor': {'distance': 0.0, 'active': 0.0, 'moving': 0.0, 'peak': 0.0, 'peakAt': 0.0, 'hist': [0.0] * (BINS + 1)}}

    @property
    def access(self):
        if self.pads:
            return 'evdev'
        if self.known and self.denied:
            return 'cursor' if self.cursor.path else 'none'
        return 'nopad' if not self.known else 'cursor' if self.cursor.path else 'none'

    def scan(self, now):
        self.last_scan = now
        found = {p['node']: p for p in parse_devices(read('/proc/bus/input/devices'))}
        for node in list(self.pads):
            if node not in found:
                os.close(self.pads[node][0])
                del self.pads[node]
        self.known = found
        for node, info in found.items():
            if node in self.pads:
                continue
            try:
                self.pads[node] = open_pad(info, time.monotonic())
                self.denied.pop(node, None)
            except OSError as e:
                self.denied[node] = str(e)
        for node in list(self.denied):
            if node not in found:
                del self.denied[node]

    def pump(self, timeout):
        fds = {fd: pad for fd, pad in self.pads.values()}
        ready = []
        if fds:
            try:
                ready, _, _ = select.select(list(fds), [], [], timeout)
            except (OSError, ValueError):
                ready = []
        else:
            time.sleep(timeout)
        for fd in ready:
            pad = fds[fd]
            try:
                data = os.read(fd, EVENT.size * 256)
            except BlockingIOError:
                continue
            except OSError:
                for node, (nfd, npad) in list(self.pads.items()):
                    if nfd == fd:
                        os.close(nfd)
                        del self.pads[node]
                        self.last_scan = 0.0
                continue
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                sec, usec, typ, code, value = EVENT.unpack_from(data, off)
                pad.feed(typ, code, value, sec + usec / 1e6)

    def gather(self, now):
        """Fold every pad's fresh counts into the minute and the day."""
        peak = 0.0
        for fd, pad in self.pads.values():
            pad.tick(time.monotonic())
            if not pad.dirty:
                continue
            pad.dirty = False
            counts, hist = pad.take()
            for k, v in counts.items():
                self.minute['counts'][k] += v
                self.today['counts'][k] += v
            for i, v in enumerate(hist):
                self.minute['hist'][i] += v
                self.today['hist'][i] += v
            if pad.peak > self.today['peak']:
                self.today['peak'], self.today['peakAt'] = pad.peak, pad.peak_at
            self.today['lastTouch'] = max(self.today.get('lastTouch', 0.0), pad.last_touch)
            peak = max(peak, pad.peak)
            pad.peak = 0.0
        self.minute['peak'] = max(self.minute['peak'], peak)
        if not self.pads and self.cursor.path:
            counts, hist = self.cursor.take()
            c = self.today['cursor']
            for k, v in counts.items():
                c[k] += v
            for i, v in enumerate(hist):
                c['hist'][i] += v
                self.minute['hist'][i] += v
            self.minute['counts']['distance'] += counts['distance']
            self.minute['counts']['active'] += counts['active']
            self.minute['counts']['moving'] += counts['moving']
            if self.cursor.peak > c['peak']:
                c['peak'], c['peakAt'] = self.cursor.peak, self.cursor.peak_at
            self.minute['peak'] = max(self.minute['peak'], self.cursor.peak)
            self.cursor.peak = 0.0

    def roll(self, now):
        if day_key(now) != self.today['day']:
            self.today = self._fresh_today(now)
        if now - self.minute['start'] >= 60:
            counts = dict(self.minute['counts'], peak=self.minute['peak'])
            if counts['frames'] or counts['distance'] > 0 or counts['touches']:
                record(self.db, self.minute['start'], counts, self.minute['hist'], self.access)
            self.minute = {'counts': zero_counters(), 'hist': [0.0] * (BINS + 1), 'peak': 0.0, 'start': now}
            atomic(STATE, 'history.json', {str(s): history(self.db, s, now) for s in (3600, 86400, 604800)})
            self.last_history = now

    def touching(self):
        return any(pad.fingers for _, pad in self.pads.values())

    def write_live(self, now):
        pads = []
        for _, pad in self.pads.values():
            pads.append({'node': pad.info['node'], 'fingers': pad.live, 'down': pad.fingers > 0, 'speed': round(pad.speed, 1), 'hz': round(pad.hz)})
        atomic(RUNTIME, 'live.json', {'ts': now, 'pads': pads,
                                      'cursor': {'speed': round(self.cursor.speed, 1)} if not self.pads and self.cursor.path else None})
        self.last_live = now

    def snapshot(self, now):
        pads = []
        for node, info in self.known.items():
            if node in self.pads:
                pads.append(dict(self.pads[node][1].facts(), readable=True))
            else:
                pads.append(dict(info, readable=False, error=self.denied.get(node, '')))
        pads.sort(key=lambda p: p['node'])
        week = week_summary(self.db, now)
        return {'ts': now, 'warm': True, 'access': self.access, 'inputGroup': in_input_group(), 'udevRule': udev_rule_present(),
                'udevRulePath': str(UDEV_RULE_PATH), 'pads': pads, 'today': self.today, 'week': week,
                'binMmS': BIN_MM_S, 'bins': BINS, 'mmPerUnitMs': MM_PER_UNIT_MS, 'pid': os.getpid(),
                'cursorSocket': bool(self.cursor.path), 'lastTouch': max([pad.last_touch for _, pad in self.pads.values()] + [self.today.get('lastTouch', 0.0)])}

    def run(self):
        with (STATE / 'collector.lock').open('w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            atomic(STATE, 'history.json', {str(s): history(self.db, s) for s in (3600, 86400, 604800)})
            while True:
                now = time.time()
                if now - self.last_scan >= 10:
                    self.scan(now)
                if self.pads:
                    self.pump(LIVE_INTERVAL if self.touching() else 0.25)
                else:
                    moving = now - self.cursor.last_move < 2.0
                    self.cursor.poll(now)
                    time.sleep(0.05 if moving else 0.2)
                now = time.time()
                self.gather(now)
                if self.touching():
                    if now - self.last_live >= LIVE_INTERVAL:
                        self.write_live(now)
                    self.live_clear_pending = True
                elif self.live_clear_pending or (not self.pads and self.cursor.path and now - self.last_live >= 0.1 and now - self.cursor.last_move < 1.0):
                    self.write_live(now)
                    self.live_clear_pending = False
                self.roll(now)
                interval = 1.0 if (self.touching() or now - self.cursor.last_move < 2.0) else 5.0
                if now - self.last_snapshot >= interval:
                    atomic(STATE, 'snapshot.json', self.snapshot(now))
                    atomic(STATE, 'today.json', self.today)
                    self.last_snapshot = now


# ---- actions --------------------------------------------------------------
def unit_text(script):
    return ('[Unit]\nDescription=Trackpad Pulse: touch telemetry and seven days of history\n'
            'After=graphical-session.target\nPartOf=graphical-session.target\n\n'
            '[Service]\nType=simple\nExecStart=/usr/bin/python3 ' + str(script) + ' daemon\n'
            'Restart=on-failure\nRestartSec=5\nUMask=0077\nNice=10\nNoNewPrivileges=yes\n\n'
            '[Install]\nWantedBy=graphical-session.target\n')


def install_service():
    units = Path.home() / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    (units / UNIT_NAME).write_text(unit_text(script))
    for args in (['daemon-reload'], ['enable', '--now', UNIT_NAME], ['restart', UNIT_NAME]):
        result = subprocess.run(['systemctl', '--user'] + args, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError('systemctl --user ' + ' '.join(args) + ': ' + (result.stderr.strip() or 'failed'))
    return {'message': 'Recorder started as ' + UNIT_NAME + '. History begins now.'}


def uninstall_service():
    units = Path.home() / '.config/systemd/user'
    subprocess.run(['systemctl', '--user', 'disable', '--now', UNIT_NAME], capture_output=True, timeout=30, check=False)
    (units / UNIT_NAME).unlink(missing_ok=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, timeout=30, check=False)
    return {'message': 'Recorder stopped and its unit removed. History files were kept.'}


def service_status():
    result = subprocess.run(['systemctl', '--user', 'is-active', UNIT_NAME], capture_output=True, text=True, timeout=10, check=False)
    return result.stdout.strip() or 'unknown'


def privileged(script, what):
    """Run one fixed root command through polkit. The text is a constant above;
    nothing from a snapshot, a device name or the panel reaches it."""
    if not shutil.which('pkexec'):
        raise RuntimeError('pkexec is not installed; run this as root instead:\n' + script)
    try:
        result = subprocess.run(['pkexec', 'sh', '-c', script], capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('The polkit prompt did not complete.')
    if result.returncode == 126 or result.returncode == 127:
        raise RuntimeError('Authorisation was cancelled; nothing changed.')
    if result.returncode:
        raise RuntimeError(what + ' failed: ' + (result.stderr.strip().splitlines() or ['no reason given'])[-1][:160])
    return result


def grant_access():
    # umask 022: this process runs at 077 for its own state files and pkexec
    # carries that into the shell, which left the rule readable by root alone.
    # udev honoured it anyway; the panel could not confirm it was there.
    script = ('set -e; umask 022; printf %s ' + shlex.quote(UDEV_RULE) + ' > ' + shlex.quote(str(UDEV_RULE_PATH))
              + '; udevadm control --reload; udevadm trigger --subsystem-match=input --action=change; udevadm settle --timeout=5 || true')
    privileged(script, 'Installing the udev rule')
    return {'message': 'Touchpad access granted through ' + str(UDEV_RULE_PATH) + '. The recorder picks it up within ten seconds.'}


def revoke_access():
    # Dropping the tag does not take back an ACL logind already granted, so
    # the caller's entry is stripped from every touchpad node explicitly.
    # PKEXEC_UID is set by pkexec itself; nothing from here names the user.
    script = ('set -e; rm -f ' + shlex.quote(str(UDEV_RULE_PATH)) + '; udevadm control --reload; '
              'for n in /dev/input/event*; do if udevadm info -q property -n "$n" 2>/dev/null | grep -q "^ID_INPUT_TOUCHPAD=1$"; '
              'then setfacl -x "u:$PKEXEC_UID" "$n" 2>/dev/null || true; fi; done')
    privileged(script, 'Removing the udev rule')
    return {'message': 'The udev rule is gone. Finger telemetry stops at the next scan; cursor-only continues.'}


def visit(link):
    url = LINKS.get(link)
    if not url:
        raise RuntimeError('Unknown link.')
    if not shutil.which('xdg-open'):
        raise RuntimeError('No xdg-open on PATH. The address is ' + url)
    subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {'message': 'Handed ' + url + ' to your browser.'}


def one_shot():
    rec = Recorder()
    rec.scan(time.time())
    value = rec.snapshot(time.time())
    value['service'] = service_status()
    for fd, _ in rec.pads.values():
        os.close(fd)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['daemon', 'snapshot', 'install-service', 'uninstall-service', 'grant-access', 'revoke-access', 'visit', 'udev-rule'])
    parser.add_argument('--link', choices=sorted(LINKS))
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if args.action == 'daemon':
            Recorder().run()
            return
        if args.action == 'udev-rule':
            print(UDEV_RULE, end='')
            return
        value = {'snapshot': one_shot, 'install-service': install_service, 'uninstall-service': uninstall_service,
                 'grant-access': grant_access, 'revoke-access': revoke_access}.get(args.action, lambda: visit(args.link))()
        print(json.dumps(value))
    except Exception as e:  # noqa: BLE001 - every failure is reported as JSON for the panel
        print(json.dumps({'error': str(e)}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
