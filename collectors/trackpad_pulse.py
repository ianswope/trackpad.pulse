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
import hashlib
import random
import sys
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
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
GESTURES_LUA = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'omarchy/toggles/hypr/zz-trackpad-pulse-gestures.lua'
HINT_INTERVAL = 600
# Where the fingers land, as a coarse grid over the pad, kept per day.
HEAT_W, HEAT_H = 32, 20

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
            'swipes3', 'swipes4', 'palms', 'distance', 'scroll', 'active', 'moving', 'frames',
            'longMoves', 'corrections', 'restrokes', 'scrollCorrections', 'scrollRestrokes')

# A wrong curve leaves fingerprints. An overshoot is a move followed at once by
# a short move back the other way; a re-stroke is a long move followed at once
# by another in the same direction, because the first ran out of pad. The
# optimizer reads their rates per speed band; nothing here changes a setting.
CORRECTION_GAP = 0.45      # s between lift and the correcting touch
CORRECTION_MAX_MM = 4.0    # the correction itself is short
LONG_MOVE_MM = 6.0         # a move worth judging
RESTROKE_GAP = 0.6
SCROLL_GAP = 0.6
SCROLL_LONG_MM = 10.0
OPTIMIZE_LOG = 'optimize-log.json'
# Gain nudges per pass, so the loop converges instead of lurching.
NUDGE_DOWN = 0.92
NUDGE_UP = 1.10


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
        self.sessions = []
        self.prev = None
        self.heat = [0] * (HEAT_W * HEAT_H)
        self.hours = [0] * 24
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
                lead = active[0]
                self.session = {'start': now, 'max': 0, 'dist': 0.0, 'palm': False, 'clicked': False, 'gap0': None, 'gapMax': 0.0,
                                'peak': 0.0, 'lead': next((k for k, s in self.slots.items() if s is lead), None),
                                'x0': lead['x'] / self.res_x, 'y0': lead['y'] / self.res_y, 'x1': lead['x'] / self.res_x, 'y1': lead['y'] / self.res_y}
                self.counts['touches'] += 1
                self.hours[time.localtime().tm_hour] += 1
            ses = self.session
            ses['max'] = max(ses['max'], fingers)
            for s in active:
                hx = min(HEAT_W - 1, max(0, int((s['x'] - self.range_x[0]) / (self.range_x[1] - self.range_x[0]) * HEAT_W)))
                hy = min(HEAT_H - 1, max(0, int((s['y'] - self.range_y[0]) / (self.range_y[1] - self.range_y[0]) * HEAT_H)))
                self.heat[hy * HEAT_W + hx] += 1
            ses['dist'] += dist
            ses['peak'] = max(ses['peak'], speed)
            leader = self.slots.get(ses['lead'])
            if leader is None or leader['id'] < 0 or leader['x'] is None:
                leader = active[0]
            ses['x1'], ses['y1'] = leader['x'] / self.res_x, leader['y'] / self.res_y
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
        rec = {'wall': time.time(), 'start': ses['start'], 'end': now, 'kind': kind, 'fingers': ses['max'],
               'duration': round(now - ses['start'], 3), 'dist': round(ses['dist'], 2), 'peak': round(ses['peak'], 1),
               'mean': round(ses['dist'] / max(0.01, now - ses['start']), 1),
               'dx': round(ses['x1'] - ses['x0'], 2), 'dy': round(ses['y1'] - ses['y0'], 2), 'flag': '', 'ref': 0.0}
        rec['flag'], rec['ref'] = flag_session(rec, self.prev)
        if kind == 'move' and rec['dist'] >= LONG_MOVE_MM:
            self.counts['longMoves'] += 1
        if rec['flag']:
            self.counts[rec['flag'] + 's'] += 1
        self.sessions.append(rec)
        self.prev = rec
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

    def take_maps(self):
        heat, self.heat = self.heat, [0] * (HEAT_W * HEAT_H)
        hours, self.hours = self.hours, [0] * 24
        return heat, hours

    def facts(self):
        f = dict(self.info)
        f.update({'width': round(self.width_mm, 1), 'height': round(self.height_mm, 1),
                  'resX': self.res_x, 'resY': self.res_y, 'unitsX': self.range_x[1] - self.range_x[0], 'unitsY': self.range_y[1] - self.range_y[0],
                  'slots': (self.axes.get('slot') or {}).get('max', 0) + 1 if self.axes.get('slot') else 1,
                  'pressure': bool(self.pressure_axis), 'major': bool(self.axes.get('major')),
                  'hz': round(self.hz_seen), 'hzNow': round(self.hz), 'fingers': self.fingers, 'speed': round(self.speed, 1), 'lastTouch': self.last_touch})
        return f


def _cos(a, b):
    la, lb = math.hypot(a['dx'], a['dy']), math.hypot(b['dx'], b['dy'])
    if la < 0.5 or lb < 0.5:
        return None
    return (a['dx'] * b['dx'] + a['dy'] * b['dy']) / (la * lb)


def flag_session(rec, prev):
    """Name what this session says about the one before it, or nothing.

    Returns (flag, reference speed): 'correction' when a long move is answered
    at once by a short move the other way (the curve moved the cursor too far
    at that speed), 'restroke' when a long move is continued at once in the
    same direction (not far enough), and the scroll equivalents.
    """
    if not prev:
        return '', 0.0
    gap = rec['start'] - prev['end']
    if rec['kind'] == 'move' and prev['kind'] == 'move':
        c = _cos(rec, prev)
        if c is not None and gap < CORRECTION_GAP and rec['dist'] < CORRECTION_MAX_MM and prev['dist'] >= LONG_MOVE_MM and c < -0.3:
            return 'correction', prev['peak']
        if c is not None and gap < RESTROKE_GAP and rec['dist'] >= LONG_MOVE_MM and prev['dist'] >= LONG_MOVE_MM and c > 0.7:
            return 'restroke', prev['peak']
    if rec['kind'] == 'scroll' and prev['kind'] == 'scroll' and gap < SCROLL_GAP and abs(rec['dy']) >= 0.5 and abs(prev['dy']) >= 0.5:
        same = (rec['dy'] > 0) == (prev['dy'] > 0)
        if not same and rec['dist'] < prev['dist'] * 0.5:
            return 'scrollCorrection', prev['peak']
        if same and rec['dist'] >= SCROLL_LONG_MM and prev['dist'] >= SCROLL_LONG_MM:
            return 'scrollRestroke', prev['peak']
    return '', 0.0


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
    db.execute('CREATE TABLE IF NOT EXISTS sessions (ts REAL, kind TEXT, fingers INTEGER, duration REAL, dist REAL, '
               'peak REAL, mean REAL, dx REAL, dy REAL, flag TEXT, ref REAL)')
    db.execute('CREATE INDEX IF NOT EXISTS sessions_ts ON sessions (ts)')
    # One row per calendar day, kept forever: a year is 365 short rows.
    db.execute('CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, touches INTEGER, taps INTEGER, clicks INTEGER, moves INTEGER, '
               'scrolls INTEGER, gestures INTEGER, palms INTEGER, distance REAL, scroll REAL, active REAL, moving REAL, peak REAL)')
    return db


def record_day(db, today):
    c = today['counts']
    db.execute('INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (today['day'], c['touches'], c['taps'] + c['taps2'] + c['taps3'], c['clicks'] + c['rightClicks'], c['moves'], c['scrolls'],
                c['pinches'] + c['swipes3'] + c['swipes4'], c['palms'], round(c['distance'], 1), round(c['scroll'], 1),
                round(c['active'], 1), round(c['moving'], 1), round(today.get('peak', 0.0), 1)))
    db.commit()


WINDOW_KEYS = ('distance', 'touches', 'taps', 'clicks', 'active')


def counts_window(c):
    """The five numbers every window carries, from a counters dict."""
    return {'distance': c.get('distance', 0.0), 'touches': c.get('touches', 0),
            'taps': c.get('taps', 0) + c.get('taps2', 0) + c.get('taps3', 0),
            'clicks': c.get('clicks', 0) + c.get('rightClicks', 0), 'active': c.get('active', 0.0)}


def windows(db, today, now, recent=None):
    """Distance, touches, taps, clicks and active time over the last minute, hour, today, week, month, year and all time.

    `recent` is the recorder's rolling list of (bucketStart, counts) ten-second
    buckets, so the minute is a true trailing 60 s rather than the calendar
    minute in progress.
    """
    live = counts_window(today['counts'])
    minute = {k: 0 for k in WINDOW_KEYS}
    for start, counts in (recent or []):
        if start >= now - 60:
            for k, val in counts_window(counts).items():
                minute[k] += val
    hour = db.execute('SELECT SUM(distance), SUM(touches), SUM(taps), SUM(clicks), SUM(active) FROM minutes WHERE ts >= ?', (now - 3600,)).fetchone()

    def span(since_day):
        row = db.execute('SELECT SUM(distance), SUM(touches), SUM(taps), SUM(clicks), SUM(active), COUNT(*) FROM days WHERE day >= ? AND day < ?', (since_day, today['day'])).fetchone()
        return {'distance': (row[0] or 0) + live['distance'], 'touches': (row[1] or 0) + live['touches'], 'taps': (row[2] or 0) + live['taps'],
                'clicks': (row[3] or 0) + live['clicks'], 'active': (row[4] or 0) + live['active'], 'days': (row[5] or 0) + 1}
    first = db.execute('SELECT MIN(day) FROM days').fetchone()[0] or today['day']
    return {'minute': minute,
            'hour': {'distance': hour[0] or 0, 'touches': hour[1] or 0, 'taps': hour[2] or 0, 'clicks': hour[3] or 0, 'active': hour[4] or 0},
            'today': live, 'week': span(day_key(now - 6 * 86400)), 'month': span(day_key(now - 29 * 86400)),
            'year': span(day_key(now - 364 * 86400)), 'all': span('0000-00-00'), 'firstDay': min(first, today['day'])}


def record_sessions(db, recs):
    if not recs:
        return
    db.executemany('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   [(r['wall'], r['kind'], r['fingers'], r['duration'], r['dist'], r['peak'], r['mean'], r['dx'], r['dy'], r['flag'], r['ref']) for r in recs])
    db.commit()


def load_sessions(db, since):
    keys = ('ts', 'kind', 'fingers', 'duration', 'dist', 'peak', 'mean', 'dx', 'dy', 'flag', 'ref')
    return [dict(zip(keys, row)) for row in db.execute('SELECT ts,kind,fingers,duration,dist,peak,mean,dx,dy,flag,ref FROM sessions WHERE ts >= ? ORDER BY ts', (since,))]


def load_hist(db, since):
    hist = [0.0] * (BINS + 1)
    for (raw,) in db.execute('SELECT hist FROM minutes WHERE ts >= ?', (since,)):
        try:
            for i, v in enumerate(json.loads(raw)[:BINS + 1]):
                hist[i] += v
        except (ValueError, TypeError):
            pass
    return hist


def record(db, ts, counts, hist, source):
    db.execute('INSERT OR REPLACE INTO minutes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (ts, counts.get('touches', 0), counts.get('taps', 0) + counts.get('taps2', 0) + counts.get('taps3', 0),
                counts.get('clicks', 0) + counts.get('rightClicks', 0), counts.get('distance', 0.0), counts.get('scroll', 0.0),
                counts.get('swipes3', 0) + counts.get('swipes4', 0) + counts.get('pinches', 0), counts.get('palms', 0),
                counts.get('active', 0.0), counts.get('peak', 0.0), json.dumps([round(v, 3) for v in hist]), source,
                read('/proc/sys/kernel/random/boot_id').strip()))
    db.execute('DELETE FROM minutes WHERE ts < ?', (ts - RETENTION,))
    db.execute('DELETE FROM sessions WHERE ts < ?', (ts - RETENTION,))
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
        self.pending_sessions = []
        self.last_hint = time.time() - HINT_INTERVAL + 60
        self.recent = deque()   # (bucketStart, counts) ten-second buckets for the trailing minute

    def _load_today(self):
        fresh = self._fresh_today(time.time())
        try:
            saved = json.loads((STATE / 'today.json').read_text())
            if saved.get('day') == fresh['day'] and isinstance(saved.get('counts'), dict):
                # Merge over a fresh record: a release that adds a counter must
                # not choke on the file the previous release wrote.
                fresh['counts'].update({k: saved['counts'].get(k, 0) for k in COUNTERS})
                for key in ('hist', 'peak', 'peakAt', 'lastTouch', 'heat', 'hours'):
                    if key in saved:
                        fresh[key] = saved[key]
                if len(fresh.get('heat') or []) != HEAT_W * HEAT_H:
                    fresh['heat'] = [0] * (HEAT_W * HEAT_H)
                if len(fresh.get('hours') or []) != 24:
                    fresh['hours'] = [0] * 24
                if isinstance(saved.get('cursor'), dict):
                    fresh['cursor'].update(saved['cursor'])
                if len(fresh['hist']) != BINS + 1:
                    fresh['hist'] = [0.0] * (BINS + 1)
        except (OSError, ValueError, TypeError):
            pass
        return fresh

    def _fresh_today(self, ts):
        return {'day': day_key(ts), 'counts': zero_counters(), 'hist': [0.0] * (BINS + 1), 'peak': 0.0, 'peakAt': 0.0, 'lastTouch': 0.0,
                'heat': [0] * (HEAT_W * HEAT_H), 'hours': [0] * 24,
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
            if pad.sessions:
                self.pending_sessions.extend(pad.sessions)
                pad.sessions = []
            heat, hours = pad.take_maps()
            for i, n in enumerate(heat):
                if n:
                    self.today['heat'][i] += n
            for i, n in enumerate(hours):
                if n:
                    self.today['hours'][i] += n
            counts, hist = pad.take()
            bucket = int(now // 10) * 10
            if not self.recent or self.recent[-1][0] != bucket:
                self.recent.append((bucket, zero_counters()))
                while self.recent and self.recent[0][0] < now - 70:
                    self.recent.popleft()
            for k, v in counts.items():
                self.minute['counts'][k] += v
                self.today['counts'][k] += v
                self.recent[-1][1][k] += v
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
            record_day(self.db, self.today)
            atomic(STATE, 'history.json', {str(s): history(self.db, s, now) for s in (3600, 86400, 604800)})
            self.last_history = now
        if now - self.last_hint >= HINT_INTERVAL:
            self.last_hint = now
            try:
                write_hint(self.db, now)
            except Exception as e:  # noqa: BLE001 - a hint is advice, never a reason to stop recording
                print('Trackpad Pulse: hint: ' + str(e), flush=True)

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
        try:
            spans = windows(self.db, self.today, now, self.recent)
        except sqlite3.Error:
            spans = {}
        return {'ts': now, 'warm': True, 'access': self.access, 'inputGroup': in_input_group(), 'udevRule': udev_rule_present(), 'windows': spans,
                'heatW': HEAT_W, 'heatH': HEAT_H,
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
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - one bad tick must not stop the recording
                    print('Trackpad Pulse: %s: %s' % (type(e).__name__, e), flush=True)
                    time.sleep(1)

    def tick(self):
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
                    try:
                        record_sessions(self.db, self.pending_sessions)
                        self.pending_sessions = []
                    except sqlite3.Error as e:
                        print('Trackpad Pulse: sessions: ' + str(e), flush=True)
                    atomic(STATE, 'snapshot.json', self.snapshot(now))
                    atomic(STATE, 'today.json', self.today)
                    self.last_snapshot = now


# ---- the optimizer --------------------------------------------------------
def percentile(hist, fraction):
    total = sum(hist)
    if total <= 0:
        return 0.0
    acc = 0.0
    for i, v in enumerate(hist):
        acc += v
        if acc / total >= fraction:
            return (i + 1) * BIN_MM_S
    return len(hist) * BIN_MM_S


def rates(sessions, start_mm, end_mm):
    """Overshoot and re-stroke rates, overall and per speed band of the move they answer."""
    moves = [s for s in sessions if s['kind'] == 'move']
    long_moves = [s for s in moves if s['dist'] >= LONG_MOVE_MM]
    corrections = [s for s in moves if s['flag'] == 'correction']
    restrokes = [s for s in moves if s['flag'] == 'restroke']
    scrolls = [s for s in sessions if s['kind'] == 'scroll']
    fast = [s for s in long_moves if s['peak'] >= end_mm]
    slow = [s for s in long_moves if s['peak'] < start_mm]

    def rate(n, d):
        return n / d if d else 0.0
    return {'sessions': len(sessions), 'moves': len(moves), 'longMoves': len(long_moves), 'corrections': len(corrections), 'restrokes': len(restrokes),
            'correctionRate': rate(len(corrections), len(long_moves)), 'restrokeRate': rate(len(restrokes), len(long_moves)),
            'fastMoves': len(fast), 'fastCorrectionRate': rate(sum(1 for s in corrections if s['ref'] >= end_mm), len(fast)),
            'slowMoves': len(slow), 'slowCorrectionRate': rate(sum(1 for s in corrections if s['ref'] < start_mm), len(slow)),
            'scrolls': len(scrolls), 'scrollReversalRate': rate(sum(1 for s in scrolls if s['flag'] == 'scrollCorrection'), len(scrolls)),
            'scrollRestrokeRate': rate(sum(1 for s in scrolls if s['flag'] == 'scrollRestroke'), len(scrolls))}


def propose(current, sessions, hist, log=None, now=None):
    """What to change and why. Pure: settings in, proposal out, nothing applied.

    current: {profile, curve:{precision,start,end,fast}, scrollFactor (slider 0.01..1),
              scrollScale, gainMaximum}. Start/End are in the editor's 0..4 units.
    """
    now = time.time() if now is None else now
    curve = dict(current.get('curve') or {'precision': 0.3, 'start': 0.8, 'end': 2.8, 'fast': 1.6})
    profile = current.get('profile', 'adaptive')
    custom = profile in ('custom', 'mac')
    gain_max = float(current.get('gainMaximum') or current.get('scrollScale') or 1)
    scroll = float(current.get('scrollFactor') or 0.4)
    moving = sum(hist)
    p45, p50, p90 = percentile(hist, 0.45), percentile(hist, 0.5), percentile(hist, 0.9)
    start_mm = curve['start'] * MM_PER_UNIT_MS if custom else p45
    end_mm = curve['end'] * MM_PER_UNIT_MS if custom else p90
    r = rates(sessions, start_mm, end_mm)
    changes, notes = [], []
    proposal = {'curve': dict(curve), 'scrollFactor': scroll, 'profile': 'custom' if custom else profile}

    enough = moving >= 300 and r['moves'] >= 200
    confidence = 'high' if moving >= 1800 and r['longMoves'] >= 600 else 'medium' if enough else 'low'

    # 1. The shape: Start where 45% of movement is slower, End at the 90th percentile.
    if moving > 0:
        new_start = round(min(3.6, max(0.0, p45 / MM_PER_UNIT_MS)), 2)
        new_end = round(min(4.0, max(new_start + 0.2, p90 / MM_PER_UNIT_MS)), 2)
        if not custom:
            base = {'precision': 0.3, 'start': new_start, 'end': new_end, 'fast': 1.6}
            factor = min(1.0, gain_max / base['fast'])
            base['precision'] = max(0.01, round(base['precision'] * factor, 4))
            base['fast'] = round(base['fast'] * factor, 4)
            proposal['curve'] = base
            proposal['profile'] = 'custom'
            changes.append({'key': 'profile', 'label': 'Profile', 'from': profile, 'to': 'custom',
                            'reason': 'A custom curve is the only place Start and End exist; gains start from the Mac-inspired preset.'})
            changes.append({'key': 'start', 'label': 'Start', 'from': None, 'to': new_start, 'reason': '45%% of your movement is slower than %.0f mm/s; below that the curve stays at precision gain.' % p45})
            changes.append({'key': 'end', 'label': 'End', 'from': None, 'to': new_end, 'reason': '90%% of your movement is slower than %.0f mm/s; the fastest tenth gets full gain.' % p90})
        else:
            if abs(new_start - curve['start']) >= 0.02:
                changes.append({'key': 'start', 'label': 'Start', 'from': curve['start'], 'to': new_start, 'reason': '45%% of your movement is slower than %.0f mm/s; Start sits there so half of what you do stays precise.' % p45})
                proposal['curve']['start'] = new_start
            if abs(new_end - curve['end']) >= 0.02:
                changes.append({'key': 'end', 'label': 'End', 'from': curve['end'], 'to': new_end, 'reason': '90%% of your movement is slower than %.0f mm/s; only the fastest tenth needs full gain.' % p90})
                proposal['curve']['end'] = new_end
            if proposal['curve']['end'] < proposal['curve']['start'] + 0.2:
                proposal['curve']['end'] = round(min(4.0, proposal['curve']['start'] + 0.2), 2)

    # 2. The gains, from the fingerprints, one bounded nudge at a time.
    if custom and enough:
        fast_up = r['restrokeRate'] > 0.12 and r['longMoves'] >= 50
        fast_down = r['fastCorrectionRate'] > 0.20 and r['fastMoves'] >= 30
        if fast_up and fast_down:
            notes.append('Re-strokes and overshoots after fast moves both run high (%.0f%% and %.0f%%); they cancel, so Fast swipes is left alone this pass.' % (r['restrokeRate'] * 100, r['fastCorrectionRate'] * 100))
        elif fast_down:
            new_fast = round(max(curve['precision'], curve['fast'] * NUDGE_DOWN), 4)
            changes.append({'key': 'fast', 'label': 'Fast swipes', 'from': curve['fast'], 'to': new_fast, 'reason': '%.0f%% of fast moves were answered by an overshoot correction; the cursor is going too far at speed.' % (r['fastCorrectionRate'] * 100)})
            proposal['curve']['fast'] = new_fast
        elif fast_up:
            new_fast = round(curve['fast'] * NUDGE_UP, 4)
            if new_fast > gain_max:
                notes.append('%.0f%% of long moves were re-strokes, but Fast swipes is already at the %.2f× ceiling; raise Device scale to go further.' % (r['restrokeRate'] * 100, gain_max))
            else:
                changes.append({'key': 'fast', 'label': 'Fast swipes', 'from': curve['fast'], 'to': new_fast, 'reason': '%.0f%% of long moves were re-strokes: the pad ran out before the cursor arrived.' % (r['restrokeRate'] * 100)})
                proposal['curve']['fast'] = new_fast
        if r['slowCorrectionRate'] > 0.25 and r['slowMoves'] >= 30:
            new_prec = round(max(0.01, curve['precision'] * NUDGE_DOWN), 4)
            changes.append({'key': 'precision', 'label': 'Precision', 'from': curve['precision'], 'to': new_prec, 'reason': '%.0f%% of slow moves were followed by a correction; fine work is overshooting.' % (r['slowCorrectionRate'] * 100)})
            proposal['curve']['precision'] = new_prec
        if proposal['curve']['fast'] < proposal['curve']['precision']:
            proposal['curve']['fast'] = proposal['curve']['precision']

    # 3. Scroll speed, same two signals.
    if r['scrolls'] >= 40:
        if r['scrollReversalRate'] > 0.25:
            new_scroll = round(max(0.01, scroll * NUDGE_DOWN), 2)
            changes.append({'key': 'scroll', 'label': 'Scroll speed', 'from': scroll, 'to': new_scroll, 'reason': '%.0f%% of scrolls were reversed at once; content is flying past.' % (r['scrollReversalRate'] * 100)})
            proposal['scrollFactor'] = new_scroll
        elif r['scrollRestrokeRate'] > 0.30:
            new_scroll = round(min(1.0, scroll * NUDGE_UP), 2)
            changes.append({'key': 'scroll', 'label': 'Scroll speed', 'from': scroll, 'to': new_scroll, 'reason': '%.0f%% of scrolls were immediately repeated in the same direction; each one is not going far enough.' % (r['scrollRestrokeRate'] * 100)})
            proposal['scrollFactor'] = new_scroll

    # 4. What the last pass did, judged by the same rates since it was applied.
    previous = None
    applied = [e for e in (log or []) if e.get('applied')]
    if applied:
        last = applied[-1]
        after = rates([s for s in sessions if s['ts'] >= last['ts']], start_mm, end_mm)
        before = last.get('evidence') or {}
        previous = {'ts': last['ts'], 'changes': last.get('changes', []), 'before': before, 'after': after,
                    'practiceBefore': last.get('practiceMedianMs'), 'practiceAfter': current.get('practiceMedianMs')}

    if not changes:
        verdict = 'nothing to change' if enough else 'nothing to change yet'
    elif not custom:
        verdict = 'first fit'
    else:
        verdict = 'fit' if all(c['key'] in ('start', 'end') for c in changes) else 'nudge'
    message = ('%.0f minutes of movement and %d moves in the window. ' % (moving / 60, r['moves'])
               + ('' if enough else 'Fewer than five minutes of movement or 200 moves: the shape can be fitted, the gains wait for more data. '))
    return {'verdict': verdict, 'confidence': confidence, 'proposal': proposal, 'changes': changes, 'notes': notes, 'message': message.strip(),
            'evidence': dict(r, movingSeconds=round(moving, 1), p45=round(p45, 1), median=round(p50, 1), p90=round(p90, 1),
                             startMm=round(start_mm, 1), endMm=round(end_mm, 1)),
            'previous': previous, 'now': now}


def load_log():
    try:
        value = json.loads((STATE / OPTIMIZE_LOG).read_text())
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def optimize(current):
    db = db_open()
    now = time.time()
    log = load_log()
    since = now - RETENTION
    applied = [e for e in log if e.get('applied')]
    # Judge with everything since the last applied pass, so an old habit does
    # not outvote a week of the new curve; the first pass sees the whole week.
    if applied:
        since = max(since, applied[-1]['ts'])
    return propose(current, load_sessions(db, since), load_hist(db, since), log, now)


def optimize_applied(entry):
    log = load_log()
    log.append({'ts': time.time(), 'applied': True, 'changes': entry.get('changes', []), 'evidence': entry.get('evidence', {}),
                'practiceMedianMs': entry.get('practiceMedianMs'), 'verdict': entry.get('verdict', '')})
    atomic(STATE, OPTIMIZE_LOG, log[-50:])
    return {'message': 'Applied and logged. The next Optimize reports whether this one helped.'}


# ---- the standing check: does the best curve differ from the one in use? ------
def current_settings():
    """The selected pad's live settings, through Trackpad Plus's own backend."""
    result = subprocess.run([sys.executable, str(PLUGIN_ROOT / 'trackpads.py'), 'state'], capture_output=True, text=True, timeout=20, check=False)
    data = json.loads(result.stdout or '{}')
    devices = [d for d in data.get('devices', []) if isinstance(d, dict)]
    if not devices:
        raise RuntimeError('no trackpad in Trackpad Plus state')
    dev = next((d for d in devices if d.get('connected')), devices[0])
    s = dev['settings']
    profile = (s.get('curve_preset') or 'custom') if s.get('accel_profile') == 'custom' else s.get('accel_profile', 'adaptive')
    scale = s.get('scroll_scale') or max(1, s.get('scroll_factor', 0.4))
    return {'device': dev.get('id'), 'profile': profile, 'curve': s.get('curve'), 'scrollFactor': s.get('scroll_factor', 0.4) / scale,
            'scrollScale': scale, 'gainMaximum': scale}


def hint_signature(changes):
    return hashlib.sha1(json.dumps(sorted([str(c['key']), str(c['to'])] for c in changes)).encode()).hexdigest()[:12]


def write_hint(db, now):
    """Re-run the optimizer against the settings in use and leave the verdict for the panel to light up."""
    current = current_settings()
    log = load_log()
    applied = [e for e in log if e.get('applied')]
    since = max(now - RETENTION, applied[-1]['ts'] if applied else 0)
    p = propose(current, load_sessions(db, since), load_hist(db, since), log, now)
    summary = ' · '.join('%s %s → %s' % (c['label'], '—' if c['from'] is None else c['from'], c['to']) for c in p['changes'])
    atomic(STATE, 'hint.json', {'ts': now, 'device': current.get('device'), 'verdict': p['verdict'], 'confidence': p['confidence'],
                                'changes': p['changes'], 'signature': hint_signature(p['changes']), 'summary': summary,
                                'movingSeconds': p['evidence'].get('movingSeconds', 0)})


# ---- themes ------------------------------------------------------------------
def theme_names():
    out = subprocess.run(['omarchy-theme-list'], capture_output=True, text=True, timeout=10, check=False).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def theme_current():
    return subprocess.run(['omarchy-theme-current'], capture_output=True, text=True, timeout=10, check=False).stdout.strip()


def pick_theme(names, current, step):
    """The next, previous or a random other theme. Pure."""
    if not names:
        raise RuntimeError('No themes installed.')
    if step == 'random':
        others = [n for n in names if n != current] or names
        return random.choice(others)
    index = names.index(current) if current in names else -1
    return names[(index + (1 if step == 'next' else -1)) % len(names)]


def theme_step(step):
    names = theme_names()
    target = pick_theme(names, theme_current(), step)
    result = subprocess.run(['omarchy-theme-set', target], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError('omarchy-theme-set failed: ' + (result.stderr.strip() or 'no reason given')[:160])
    return {'message': 'Theme: ' + target}


# ---- gestures ----------------------------------------------------------------
# Every slot Hyprland offers for three and four fingers, and every action the
# catalogue knows. The panel only ever sends ids; the Lua below is built from
# these constants and nothing else reaches Hyprland's config.
FINGERS = (3, 4)
DIRECTIONS = ('left', 'right', 'up', 'down', 'pinchin', 'pinchout')
SLOTS = ['%d-%s' % (f, d) for f in FINGERS for d in DIRECTIONS]
AXIS = {'left': 'horizontal', 'right': 'horizontal', 'up': 'vertical', 'down': 'vertical'}
PARTNER = {'left': 'right', 'right': 'left', 'up': 'down', 'down': 'up'}
COLLECTOR = str(Path(__file__).resolve())


def _exec(cmd):
    return {'kind': 'exec', 'cmd': cmd}


def _dispatch(lua):
    return {'kind': 'dispatch', 'lua': lua}


def _native(action, **extra):
    return dict({'kind': 'native', 'action': action}, **extra)


CATALOGUE = [
    {'id': 'none', 'group': 'Nothing', 'label': 'Nothing', 'hint': 'Leave this gesture unassigned.', 'spec': {'kind': 'none'}},
    # Workspaces
    {'id': 'ws-slide', 'group': 'Workspaces', 'label': 'Slide between workspaces', 'hint': 'Follows your fingers, animated, like macOS. Takes both directions of the axis.', 'pair': True, 'spec': _native('workspace')},
    {'id': 'ws-next', 'group': 'Workspaces', 'label': 'Next workspace', 'hint': 'Jump one workspace to the right.', 'spec': _dispatch('hl.dsp.focus({ workspace = "e+1" })')},
    {'id': 'ws-prev', 'group': 'Workspaces', 'label': 'Previous workspace', 'hint': 'Jump one workspace to the left.', 'spec': _dispatch('hl.dsp.focus({ workspace = "e-1" })')},
    {'id': 'ws-last', 'group': 'Workspaces', 'label': 'Last used workspace', 'hint': 'Back to where you just were.', 'spec': _dispatch('hl.dsp.focus({ workspace = "previous" })')},
    {'id': 'ws-scratch', 'group': 'Workspaces', 'label': 'Toggle the scratchpad', 'hint': 'The special workspace, in and out.', 'spec': _native('special', workspace_name='scratchpad')},
    {'id': 'ws-layout', 'group': 'Workspaces', 'label': 'Toggle workspace layout', 'hint': 'Tiled or scrolling, per workspace.', 'spec': _exec('omarchy-hyprland-workspace-layout-toggle')},
    {'id': 'scroll-move', 'group': 'Workspaces', 'label': 'Scroll the tape', 'hint': 'Move along the scrolling layout, 1:1. Takes both directions of the axis.', 'pair': True, 'spec': _native('scroll_move')},
    # Windows
    {'id': 'win-fullscreen', 'group': 'Windows', 'label': 'Fullscreen window', 'hint': 'Toggle the active window fullscreen.', 'spec': _native('fullscreen')},
    {'id': 'win-maximize', 'group': 'Windows', 'label': 'Maximize window', 'hint': 'Fill the screen but keep the bar.', 'spec': _native('fullscreen', mode='maximize')},
    {'id': 'win-close', 'group': 'Windows', 'label': 'Close window', 'hint': 'Close the active window.', 'spec': _native('close')},
    {'id': 'win-float', 'group': 'Windows', 'label': 'Float or tile window', 'hint': 'Pop the window out of the tiling, or back in.', 'spec': _native('float')},
    {'id': 'win-move', 'group': 'Windows', 'label': 'Move window', 'hint': 'Drag the active window with the gesture. Takes both directions of the axis.', 'pair': True, 'spec': _native('move')},
    {'id': 'win-resize', 'group': 'Windows', 'label': 'Resize window', 'hint': 'Resize the active window with the gesture. Takes both directions of the axis.', 'pair': True, 'spec': _native('resize')},
    {'id': 'focus-left', 'group': 'Windows', 'label': 'Focus window to the left', 'hint': 'Move focus one window left.', 'spec': _dispatch('hl.dsp.focus({ direction = "l" })')},
    {'id': 'focus-right', 'group': 'Windows', 'label': 'Focus window to the right', 'hint': 'Move focus one window right.', 'spec': _dispatch('hl.dsp.focus({ direction = "r" })')},
    {'id': 'focus-up', 'group': 'Windows', 'label': 'Focus window above', 'hint': 'Move focus one window up.', 'spec': _dispatch('hl.dsp.focus({ direction = "u" })')},
    {'id': 'focus-down', 'group': 'Windows', 'label': 'Focus window below', 'hint': 'Move focus one window down.', 'spec': _dispatch('hl.dsp.focus({ direction = "d" })')},
    {'id': 'win-gaps', 'group': 'Windows', 'label': 'Toggle window gaps', 'hint': 'Gaps on or off, everywhere.', 'spec': _exec('omarchy-hyprland-window-gaps-toggle')},
    {'id': 'win-transparency', 'group': 'Windows', 'label': 'Toggle window transparency', 'hint': 'See through the active window, or not.', 'spec': _exec('omarchy-hyprland-window-transparency-toggle')},
    # Themes & backgrounds
    {'id': 'theme-next', 'group': 'Themes & backgrounds', 'label': 'Next theme', 'hint': 'Step through your installed themes.', 'spec': _exec(shlex.join([sys.executable, COLLECTOR, 'theme-next']))},
    {'id': 'theme-prev', 'group': 'Themes & backgrounds', 'label': 'Previous theme', 'hint': 'Step back through your themes.', 'spec': _exec(shlex.join([sys.executable, COLLECTOR, 'theme-prev']))},
    {'id': 'theme-random', 'group': 'Themes & backgrounds', 'label': 'Random theme', 'hint': 'Surprise me.', 'spec': _exec(shlex.join([sys.executable, COLLECTOR, 'theme-random']))},
    {'id': 'theme-pick', 'group': 'Themes & backgrounds', 'label': 'Theme picker', 'hint': "Open Omarchy's theme switcher.", 'spec': _exec('omarchy-theme-switcher')},
    {'id': 'bg-next', 'group': 'Themes & backgrounds', 'label': 'Next background', 'hint': "The current theme's next background.", 'spec': _exec('omarchy-theme-bg-next')},
    {'id': 'bg-pick', 'group': 'Themes & backgrounds', 'label': 'Background picker', 'hint': "Open Omarchy's background switcher.", 'spec': _exec('omarchy-theme-bg-switcher')},
    {'id': 'nightlight', 'group': 'Themes & backgrounds', 'label': 'Toggle night light', 'hint': 'Warm the screen, or cool it.', 'spec': _exec('omarchy-toggle-nightlight')},
    # Omarchy
    {'id': 'menu', 'group': 'Omarchy', 'label': 'Omarchy menu', 'hint': 'The main menu.', 'spec': _exec('omarchy-menu toggle')},
    {'id': 'emoji', 'group': 'Omarchy', 'label': 'Emoji picker', 'hint': 'Search and insert an emoji.', 'spec': _exec('omarchy-menu-emoji')},
    {'id': 'clipboard', 'group': 'Omarchy', 'label': 'Clipboard history', 'hint': 'Pick something you copied earlier.', 'spec': _exec('omarchy-menu-clipboard')},
    {'id': 'keybindings', 'group': 'Omarchy', 'label': 'Keybindings', 'hint': 'Search every shortcut.', 'spec': _exec('omarchy-menu-keybindings')},
    {'id': 'shot-region', 'group': 'Omarchy', 'label': 'Screenshot a region', 'hint': 'Select an area and capture it.', 'spec': _exec('omarchy-capture-screenshot region')},
    {'id': 'shot-full', 'group': 'Omarchy', 'label': 'Screenshot the screen', 'hint': 'Capture everything.', 'spec': _exec('omarchy-capture-screenshot fullscreen')},
    {'id': 'record', 'group': 'Omarchy', 'label': 'Screen recording', 'hint': 'Start or stop recording.', 'spec': _exec('omarchy-capture-screenrecording')},
    {'id': 'lock', 'group': 'Omarchy', 'label': 'Lock the screen', 'hint': 'Lock now.', 'spec': _exec('omarchy-system-lock')},
    {'id': 'screensaver', 'group': 'Omarchy', 'label': 'Screensaver', 'hint': 'Start the Omarchy screensaver.', 'spec': _exec('omarchy-launch-screensaver')},
    {'id': 'bar', 'group': 'Omarchy', 'label': 'Toggle the bar', 'hint': 'Hide or show the top bar.', 'spec': _exec('omarchy-toggle-bar toggle')},
    {'id': 'dnd', 'group': 'Omarchy', 'label': 'Do not disturb', 'hint': 'Silence notifications, or let them back.', 'spec': _exec('omarchy-toggle-notification-silencing')},
    {'id': 'terminal', 'group': 'Omarchy', 'label': 'New terminal', 'hint': 'Open a terminal.', 'spec': _exec('omarchy-launch-terminal')},
    {'id': 'browser', 'group': 'Omarchy', 'label': 'Browser', 'hint': 'Open or focus the browser.', 'spec': _exec('omarchy-launch-browser')},
    {'id': 'files', 'group': 'Omarchy', 'label': 'Files', 'hint': 'Open the file manager.', 'spec': _exec('omarchy-launch-nautilus')},
    # Media & audio
    {'id': 'vol-up', 'group': 'Media & audio', 'label': 'Volume up', 'hint': 'Raise the volume with the OSD.', 'spec': _exec('omarchy-audio-output-volume raise')},
    {'id': 'vol-down', 'group': 'Media & audio', 'label': 'Volume down', 'hint': 'Lower the volume with the OSD.', 'spec': _exec('omarchy-audio-output-volume lower')},
    {'id': 'mute', 'group': 'Media & audio', 'label': 'Mute', 'hint': 'Toggle mute.', 'spec': _exec('omarchy-audio-output-volume mute-toggle')},
    {'id': 'mic-mute', 'group': 'Media & audio', 'label': 'Mute microphone', 'hint': 'Toggle the mic.', 'spec': _exec('omarchy-audio-input-mute')},
    {'id': 'audio-switch', 'group': 'Media & audio', 'label': 'Switch audio output', 'hint': 'Next speaker or headset.', 'spec': _exec('omarchy-audio-output-switch')},
    {'id': 'bright-up', 'group': 'Media & audio', 'label': 'Brightness up', 'hint': 'Screen brighter by 5%.', 'requires': 'omarchy-brightness-display', 'spec': _exec('omarchy-brightness-display +5%')},
    {'id': 'bright-down', 'group': 'Media & audio', 'label': 'Brightness down', 'hint': 'Screen dimmer by 5%.', 'requires': 'omarchy-brightness-display', 'spec': _exec('omarchy-brightness-display 5%-')},
    {'id': 'play-pause', 'group': 'Media & audio', 'label': 'Play / pause', 'hint': 'Needs playerctl.', 'requires': 'playerctl', 'spec': _exec('playerctl play-pause')},
    {'id': 'track-next', 'group': 'Media & audio', 'label': 'Next track', 'hint': 'Needs playerctl.', 'requires': 'playerctl', 'spec': _exec('playerctl next')},
    {'id': 'track-prev', 'group': 'Media & audio', 'label': 'Previous track', 'hint': 'Needs playerctl.', 'requires': 'playerctl', 'spec': _exec('playerctl previous')},
    # Zoom
    {'id': 'zoom', 'group': 'Zoom', 'label': 'Zoom the screen ×2', 'hint': 'Toggle a 2× zoom at the cursor. Made for pinches.', 'spec': _native('cursor_zoom', zoom_level=2)},
    {'id': 'zoom-live', 'group': 'Zoom', 'label': 'Zoom with the pinch', 'hint': 'Zoom follows the pinch live. Made for pinches.', 'spec': _native('cursor_zoom', zoom_level=1, mode='live')},
    # Trackpad Pulse
    {'id': 'pulse-open', 'group': 'Trackpad Pulse', 'label': 'Open Trackpad Pulse', 'hint': 'The dashboard.', 'spec': _exec('omarchy-shell nixfred.trackpad-pulse toggle')},
    {'id': 'pulse-optimize', 'group': 'Trackpad Pulse', 'label': 'Optimize for my hand', 'hint': 'A fresh proposal on Pointer feel.', 'spec': _exec('omarchy-shell nixfred.trackpad-pulse optimize')},
    {'id': 'pad-off', 'group': 'Trackpad Pulse', 'label': 'Trackpad off', 'hint': 'Switch the pad off; turn it back on from the bar icon.', 'spec': _exec('omarchy-shell nixfred.trackpad-pulse enable false')},
]
CATALOGUE_BY_ID = {a['id']: a for a in CATALOGUE}
DEFAULT_GESTURES = {'3-left': 'ws-slide', '3-right': 'ws-slide', '3-up': 'win-fullscreen', '3-down': 'ws-scratch',
                    '3-pinchin': 'none', '3-pinchout': 'none',
                    '4-left': 'theme-prev', '4-right': 'theme-next', '4-up': 'bg-next', '4-down': 'menu',
                    '4-pinchin': 'none', '4-pinchout': 'zoom'}


def gesture_catalogue():
    """The catalogue, minus actions whose command is not installed here."""
    out = []
    for a in CATALOGUE:
        entry = {k: a[k] for k in ('id', 'group', 'label', 'hint')}
        entry['pair'] = bool(a.get('pair'))
        entry['available'] = not a.get('requires') or bool(shutil.which(a['requires']))
        out.append(entry)
    current = None
    try:
        current = normalize_gestures(json.loads((STATE / 'gestures.json').read_text()))
    except (OSError, ValueError, RuntimeError):
        current = None
    return {'actions': out, 'slots': SLOTS, 'defaults': DEFAULT_GESTURES, 'file': str(GESTURES_LUA),
            'applied': GESTURES_LUA.exists(), 'current': current if GESTURES_LUA.exists() else None}


def lua_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def normalize_gestures(assignments):
    """Validate a slot→action map from the panel. Unknown slots or actions are refused, not guessed."""
    if not isinstance(assignments, dict):
        raise RuntimeError('Gestures must be an object of slot → action.')
    out = {slot: 'none' for slot in SLOTS}
    for slot, action in assignments.items():
        if slot not in out:
            raise RuntimeError('Unknown gesture slot: ' + str(slot)[:40])
        if action not in CATALOGUE_BY_ID:
            raise RuntimeError('Unknown action: ' + str(action)[:40])
        out[slot] = action
    # A pair action owns its whole axis: both directions say the same thing.
    for slot, action in list(out.items()):
        fingers, direction = slot.split('-')
        if direction in PARTNER and CATALOGUE_BY_ID[action].get('pair'):
            out['%s-%s' % (fingers, PARTNER[direction])] = action
    return out


def gestures_lua(assignments):
    """The Hyprland Lua for a validated map. Pair actions are emitted once per axis."""
    lines = ['do -- Managed by nixfred.trackpad-pulse. Change gestures in Trackpad Pulse.']
    emitted = set()
    for slot in SLOTS:
        action = CATALOGUE_BY_ID[assignments.get(slot, 'none')]
        spec = action['spec']
        if spec['kind'] == 'none':
            continue
        fingers, direction = slot.split('-')
        if action.get('pair'):
            direction = AXIS.get(direction, direction)
            if (fingers, direction) in emitted:
                continue
            emitted.add((fingers, direction))
        fields = ['fingers = %s' % fingers, 'direction = %s' % lua_quote(direction)]
        if spec['kind'] == 'native':
            fields.append('action = %s' % lua_quote(spec['action']))
            for key in ('workspace_name', 'mode'):
                if key in spec:
                    fields.append('%s = %s' % (key, lua_quote(spec[key])))
            if 'zoom_level' in spec:
                fields.append('zoom_level = %s' % spec['zoom_level'])
        elif spec['kind'] == 'exec':
            fields.append('action = function() hl.exec_cmd(%s) end' % lua_quote(spec['cmd']))
        else:
            fields.append('action = function() hl.dispatch(%s) end' % spec['lua'])
        lines.append('hl.gesture({ ' + ', '.join(fields) + ' })')
    return '\n'.join(lines + ['end']) + '\n'


def _reload_hyprland():
    sys.path.insert(0, str(PLUGIN_ROOT))
    import trackpads  # noqa: E402 - Trackpad Plus's bounded hyprctl and hardened writer
    trackpads.hypr('reload', 'config-only')


def gestures_apply(assignments):
    clean = normalize_gestures(assignments)
    sys.path.insert(0, str(PLUGIN_ROOT))
    import trackpads  # noqa: E402
    trackpads.atomic_write(GESTURES_LUA, gestures_lua(clean))
    _reload_hyprland()
    atomic(STATE, 'gestures.json', clean)
    live = sum(1 for a in clean.values() if a != 'none')
    return {'message': '%d gesture%s live in Hyprland.' % (live, '' if live == 1 else 's'), 'gestures': clean}


def gestures_remove():
    for path in (GESTURES_LUA, STATE / 'gestures.json'):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    _reload_hyprland()
    return {'message': 'Gesture file removed; Hyprland reloaded. Your own input.lua gestures, if any, are all that is left.'}


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
    parser.add_argument('action', choices=['daemon', 'snapshot', 'install-service', 'uninstall-service', 'grant-access', 'revoke-access', 'visit', 'udev-rule',
                                           'optimize', 'optimize-applied', 'hint', 'gestures-catalogue', 'gestures-apply', 'gestures-remove',
                                           'theme-next', 'theme-prev', 'theme-random'])
    parser.add_argument('payload', nargs='?', default='{}', help='JSON for optimize / optimize-applied')
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
        if args.action in ('optimize', 'optimize-applied', 'gestures-apply'):
            try:
                payload = json.loads(args.payload or '{}')
            except ValueError:
                raise RuntimeError('This action needs a JSON payload.')
            if not isinstance(payload, dict):
                raise RuntimeError('The payload must be an object.')
            value = {'optimize': optimize, 'optimize-applied': optimize_applied, 'gestures-apply': gestures_apply}[args.action](payload)
        elif args.action == 'hint':
            write_hint(db_open(), time.time())
            value = json.loads((STATE / 'hint.json').read_text())
        elif args.action == 'gestures-catalogue':
            value = gesture_catalogue()
        elif args.action == 'gestures-remove':
            value = gestures_remove()
        elif args.action.startswith('theme-'):
            value = theme_step(args.action[6:])
        else:
            value = {'snapshot': one_shot, 'install-service': install_service, 'uninstall-service': uninstall_service,
                     'grant-access': grant_access, 'revoke-access': revoke_access}.get(args.action, lambda: visit(args.link))()
        print(json.dumps(value))
    except Exception as e:  # noqa: BLE001 - every failure is reported as JSON for the panel
        print(json.dumps({'error': str(e)}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
