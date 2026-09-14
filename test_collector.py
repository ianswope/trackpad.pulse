"""Feed the Trackpad Pulse classifier synthetic evdev frames and check what it counts.

No device, no root, no daemon: Pad is pure. The frames below are what a
hid-multitouch touchpad emits, at a made-up 31 units/mm so millimetres are
easy to reason about (31 units = 1 mm).
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'collectors'))
import trackpad_pulse as tp  # noqa: E402

RES = 31
AXES = {'x': {'min': 0, 'max': 4010, 'res': RES}, 'y': {'min': 0, 'max': 2468, 'res': RES},
        'slot': {'min': 0, 'max': 4, 'res': 0}, 'pressure': None, 'major': None}


def pad(now=100.0):
    return tp.Pad({'node': '/dev/input/event11', 'name': 'Test Touchpad', 'multitouch': True}, AXES, now)


class Finger:
    """Drives one slot through down / move / up, emitting the frames a kernel would."""

    def __init__(self, p, slot, tracking):
        self.p, self.slot, self.tracking = p, slot, tracking

    def down(self, x, y, now, tool=0):
        self.p.feed(tp.EV_ABS, tp.ABS_MT_SLOT, self.slot, now)
        self.p.feed(tp.EV_ABS, tp.ABS_MT_TRACKING_ID, self.tracking, now)
        self.p.feed(tp.EV_ABS, tp.ABS_MT_POSITION_X, x, now)
        self.p.feed(tp.EV_ABS, tp.ABS_MT_POSITION_Y, y, now)
        if tool:
            self.p.feed(tp.EV_ABS, tp.ABS_MT_TOOL_TYPE, tool, now)

    def move(self, x, y, now):
        self.p.feed(tp.EV_ABS, tp.ABS_MT_SLOT, self.slot, now)
        self.p.feed(tp.EV_ABS, tp.ABS_MT_POSITION_X, x, now)
        self.p.feed(tp.EV_ABS, tp.ABS_MT_POSITION_Y, y, now)

    def up(self, now):
        self.p.feed(tp.EV_ABS, tp.ABS_MT_SLOT, self.slot, now)
        self.p.feed(tp.EV_ABS, tp.ABS_MT_TRACKING_ID, -1, now)


def sync(p, now, touch=None):
    if touch is not None:
        p.feed(tp.EV_KEY, tp.BTN_TOUCH, 1 if touch else 0, now)
    p.feed(tp.EV_SYN, tp.SYN_REPORT, 0, now)


class ClassifierTests(unittest.TestCase):
    def test_one_finger_tap_counts_a_tap_and_no_distance(self):
        p = pad()
        f = Finger(p, 0, 1)
        f.down(1000, 1000, 100.00); sync(p, 100.00, True)
        f.move(1010, 1004, 100.05); sync(p, 100.05)
        f.up(100.10); sync(p, 100.10, False)
        self.assertEqual(p.counts['touches'], 1)
        self.assertEqual(p.counts['taps'], 1)
        self.assertEqual(p.counts['moves'], 0)
        self.assertLess(p.counts['distance'], 1.0)
        self.assertEqual(p.live, [], 'lifting every finger clears the live map')

    def test_one_finger_drag_measures_millimetres_and_speed(self):
        p = pad()
        f = Finger(p, 0, 1)
        f.down(0, 1000, 100.00); sync(p, 100.00, True)
        # 20 mm to the right over 0.2 s in 8 Hz-ish frames: 100 mm/s.
        for i in range(1, 11):
            f.move(i * 2 * RES, 1000, 100.00 + i * 0.02); sync(p, 100.00 + i * 0.02)
        f.up(100.30); sync(p, 100.30, False)
        self.assertEqual(p.counts['moves'], 1)
        self.assertAlmostEqual(p.counts['distance'], 20.0, places=3)
        self.assertAlmostEqual(p.peak, 100.0, places=3)
        self.assertAlmostEqual(sum(p.hist), 0.2, places=6, msg='speed histogram is time-weighted')
        # 100 mm/s sits exactly on the 95–100 / 100–105 bin edge, and the
        # float timestamps land it on either side; both bins together hold it.
        edge = int(100 / tp.BIN_MM_S)
        self.assertAlmostEqual(p.hist[edge - 1] + p.hist[edge], 0.2, places=6)
        self.assertAlmostEqual(p.counts['moving'], 0.2, places=6)

    def test_two_finger_scroll_and_pinch_are_told_apart(self):
        p = pad()
        a, b = Finger(p, 0, 1), Finger(p, 1, 2)
        a.down(1000, 500, 100.0); b.down(1000 + 15 * RES, 500, 100.0); sync(p, 100.0, True)
        p.feed(tp.EV_KEY, tp.BTN_TOOL_DOUBLETAP, 1, 100.0)
        for i in range(1, 6):
            a.move(1000, 500 + i * 3 * RES, 100.0 + i * 0.05); b.move(1000 + 15 * RES, 500 + i * 3 * RES, 100.0 + i * 0.05); sync(p, 100.0 + i * 0.05)
        a.up(100.4); b.up(100.4); sync(p, 100.4, False)
        self.assertEqual(p.counts['scrolls'], 1)
        self.assertEqual(p.counts['pinches'], 0)
        self.assertAlmostEqual(p.counts['scroll'], 15.0, places=3)
        a, b = Finger(p, 0, 3), Finger(p, 1, 4)
        a.down(1000, 500, 101.0); b.down(1000 + 10 * RES, 500, 101.0); sync(p, 101.0, True)
        for i in range(1, 6):
            a.move(1000 - i * 2 * RES, 500, 101.0 + i * 0.05); b.move(1000 + (10 + i * 2) * RES, 500, 101.0 + i * 0.05); sync(p, 101.0 + i * 0.05)
        a.up(101.4); b.up(101.4); sync(p, 101.4, False)
        self.assertEqual(p.counts['pinches'], 1)
        self.assertEqual(p.counts['scrolls'], 1)

    def test_three_and_four_finger_swipes(self):
        p = pad()
        fingers = [Finger(p, i, 10 + i) for i in range(3)]
        for i, f in enumerate(fingers):
            f.down(500 + i * 20 * RES, 1000, 100.0)
        sync(p, 100.0, True)
        for step in range(1, 5):
            for i, f in enumerate(fingers):
                f.move(500 + i * 20 * RES + step * 4 * RES, 1000, 100.0 + step * 0.05)
            sync(p, 100.0 + step * 0.05)
        for f in fingers:
            f.up(100.3)
        sync(p, 100.3, False)
        self.assertEqual(p.counts['swipes3'], 1)
        fingers = [Finger(p, i, 20 + i) for i in range(4)]
        for i, f in enumerate(fingers):
            f.down(500 + i * 15 * RES, 1000, 101.0)
        sync(p, 101.0, True)
        for step in range(1, 5):
            for i, f in enumerate(fingers):
                f.move(500 + i * 15 * RES, 1000 + step * 4 * RES, 101.0 + step * 0.05)
            sync(p, 101.0 + step * 0.05)
        for f in fingers:
            f.up(101.3)
        sync(p, 101.3, False)
        self.assertEqual(p.counts['swipes4'], 1)
        self.assertEqual(p.counts['touches'], 2)

    def test_palm_is_counted_not_classified_as_a_move(self):
        p = pad()
        f = Finger(p, 0, 1)
        f.down(2000, 2000, 100.0, tool=tp.MT_TOOL_PALM); sync(p, 100.0, True)
        f.move(2000 + 10 * RES, 2000, 100.2); sync(p, 100.2)
        f.up(100.4); sync(p, 100.4, False)
        self.assertEqual(p.counts['palms'], 1)
        self.assertEqual(p.counts['moves'], 0)

    def test_physical_click_counts_once_per_press(self):
        p = pad()
        f = Finger(p, 0, 1)
        f.down(1000, 1000, 100.0); sync(p, 100.0, True)
        p.feed(tp.EV_KEY, tp.BTN_LEFT, 1, 100.1); sync(p, 100.1)
        p.feed(tp.EV_KEY, tp.BTN_LEFT, 1, 100.15); sync(p, 100.15)  # repeated press report
        p.feed(tp.EV_KEY, tp.BTN_LEFT, 0, 100.2); sync(p, 100.2)
        f.up(100.3); sync(p, 100.3, False)
        self.assertEqual(p.counts['clicks'], 1)
        self.assertEqual(p.counts['taps'], 0, 'a press is not a tap')

    def test_live_map_is_normalised_and_report_rate_is_measured(self):
        p = pad()
        f = Finger(p, 0, 1)
        f.down(2005, 1234, 100.0); sync(p, 100.0, True)
        self.assertEqual(len(p.live), 1)
        self.assertAlmostEqual(p.live[0]['x'], 0.5, places=2)
        self.assertAlmostEqual(p.live[0]['y'], 0.5, places=2)
        for i in range(1, 126):
            f.move(2005 + i, 1234, 100.0 + i / 125); sync(p, 100.0 + i / 125)
        self.assertGreaterEqual(p.hz, 120)
        self.assertLessEqual(p.hz, 126)
        facts = p.facts()
        self.assertAlmostEqual(facts['width'], 4010 / RES, places=1)
        self.assertEqual(facts['slots'], 5)

    def test_take_hands_over_and_resets(self):
        p = pad()
        f = Finger(p, 0, 1)
        f.down(0, 0, 100.0); sync(p, 100.0, True)
        f.move(10 * RES, 0, 100.1); sync(p, 100.1)
        f.up(100.2); sync(p, 100.2, False)
        counts, hist = p.take()
        self.assertEqual(counts['touches'], 1)
        self.assertEqual(p.counts['touches'], 0)
        self.assertEqual(sum(p.hist), 0.0)
        self.assertEqual(len(hist), tp.BINS + 1)

    def test_stuck_session_closes_on_tick(self):
        p = pad()
        f = Finger(p, 0, 1)
        f.down(0, 0, 100.0); sync(p, 100.0, True)
        f.up(100.2); sync(p, 100.2, False)
        self.assertIsNone(p.session)
        f.down(0, 0, 101.0); sync(p, 101.0, True)
        p.slots[0]['id'] = -1  # a lost lift event
        p.tick(102.0)
        self.assertIsNone(p.session)
        self.assertEqual(p.live, [])


class DiscoveryTests(unittest.TestCase):
    SAMPLE = '''I: Bus=0018 Vendor=04f3 Product=3352 Version=0100
N: Name="ELAN030D:00 04F3:3352 Mouse"
P: Phys=i2c-ELAN030D:00
H: Handlers=event10 mouse0
B: PROP=0
B: EV=17
B: KEY=30000 0 0 0 0
B: REL=3

I: Bus=0018 Vendor=04f3 Product=3352 Version=0100
N: Name="ELAN030D:00 04F3:3352 Touchpad"
P: Phys=i2c-ELAN030D:00
H: Handlers=event11 mouse1
B: PROP=5
B: EV=1b
B: KEY=e520 10000 0 0 0 0
B: ABS=2e0800000000003
B: MSC=20

I: Bus=0018 Vendor=04f3 Product=3352 Version=0100
N: Name="ELAN030D:00 04F3:3352 Consumer Control"
H: Handlers=kbd event12
B: PROP=0
B: KEY=33e40 0 0 808003072fd025 bf84444200000000 1 13007300138000 43fa00404c00 9e168000004400 10000002
B: ABS=100000000

I: Bus=0003 Vendor=04f3 Product=2d55 Version=0111
N: Name="ELAN Touchscreen"
H: Handlers=event5
B: PROP=2
B: ABS=2e0800000000003

I: Bus=0005 Vendor=05ac Product=0265 Version=0000
N: Name="Apple Inc. Magic Trackpad 2"
H: Handlers=event20 mouse3
B: PROP=5
B: KEY=e520 10000 0 0 0 0
B: ABS=2e0800000000003
'''

    def test_only_indirect_multitouch_pads_are_found(self):
        pads = tp.parse_devices(self.SAMPLE)
        self.assertEqual([p['node'] for p in pads], ['/dev/input/event11', '/dev/input/event20'])
        self.assertEqual(pads[0]['name'], 'ELAN030D:00 04F3:3352 Touchpad')
        self.assertTrue(pads[0]['multitouch'])
        self.assertFalse(pads[0]['pressure'])

    def test_bitmap_words_are_most_significant_first(self):
        self.assertTrue(tp.bit('e520 10000 0 0 0 0', tp.BTN_TOUCH))
        self.assertTrue(tp.bit('e520 10000 0 0 0 0', tp.BTN_TOOL_FINGER))
        self.assertFalse(tp.bit('e520 10000 0 0 0 0', tp.BTN_LEFT + 1))
        self.assertTrue(tp.bit('2e0800000000003', tp.ABS_MT_SLOT))
        self.assertFalse(tp.bit('2e0800000000003', tp.ABS_MT_PRESSURE))


class HistoryTests(unittest.TestCase):
    def test_buckets_sum_touches_and_keep_the_peak(self):
        with tempfile.TemporaryDirectory() as directory:
            tp.STATE = Path(directory)
            db = tp.db_open()
            now = 1_000_000.0
            for minute in range(30):
                counts = dict(tp.zero_counters(), touches=2, taps=1, distance=10.0, active=5.0, peak=50.0 + minute)
                tp.record(db, now - 1800 + minute * 60, counts, [0.1] * (tp.BINS + 1), 'evdev')
            hour = tp.history(db, 3600, now)
            self.assertEqual(hour['touches'], 60)
            self.assertEqual(hour['peak'], 79.0)
            self.assertEqual(len(hour['points'][0]), 7)
            week = tp.week_summary(db, now)
            self.assertEqual(week['touches'], 60)
            self.assertEqual(week['minutes'], 30)
            self.assertAlmostEqual(week['hist'][0], 3.0, places=6)
            tp.record(db, now - tp.RETENTION - 60, dict(tp.zero_counters(), touches=1), [0.0] * (tp.BINS + 1), 'evdev')
            tp.record(db, now, dict(tp.zero_counters(), touches=1), [0.0] * (tp.BINS + 1), 'evdev')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM minutes WHERE ts < ?', (now - tp.RETENTION,)).fetchone()[0], 0, 'rows older than seven days are pruned')

    def test_classify_edges(self):
        self.assertEqual(tp.classify({'start': 0, 'max': 1, 'dist': 0.5, 'palm': False, 'clicked': False, 'gap0': None, 'gapMax': 0}, 0.1), 'tap')
        self.assertEqual(tp.classify({'start': 0, 'max': 2, 'dist': 0.5, 'palm': False, 'clicked': False, 'gap0': 10, 'gapMax': 0}, 0.1), 'tap2')
        self.assertEqual(tp.classify({'start': 0, 'max': 1, 'dist': 0.5, 'palm': False, 'clicked': True, 'gap0': None, 'gapMax': 0}, 0.1), 'move')
        self.assertEqual(tp.classify({'start': 0, 'max': 2, 'dist': 2.0, 'palm': False, 'clicked': False, 'gap0': 10, 'gapMax': 0}, 1.0), 'move')
        self.assertEqual(tp.classify({'start': 0, 'max': 5, 'dist': 20.0, 'palm': False, 'clicked': False, 'gap0': None, 'gapMax': 0}, 1.0), 'swipe4')

    def test_unit_text_points_at_this_script(self):
        text = tp.unit_text(Path('/x/y/trackpad_pulse.py'))
        self.assertIn('ExecStart=/usr/bin/python3 /x/y/trackpad_pulse.py daemon', text)
        self.assertIn('NoNewPrivileges=yes', text)
        self.assertEqual(json.loads(json.dumps(tp.zero_counters()))['touches'], 0)


class FingerprintTests(unittest.TestCase):
    """Overshoot corrections and re-strokes are named as sessions end, against the session before."""

    def drag(self, p, tracking, x0, y0, x1, y1, t0, seconds, steps=8):
        f = Finger(p, 0, tracking)
        f.down(x0, y0, t0); sync(p, t0, True)
        for i in range(1, steps + 1):
            t = t0 + seconds * i / steps
            f.move(int(x0 + (x1 - x0) * i / steps), int(y0 + (y1 - y0) * i / steps), t); sync(p, t)
        f.up(t0 + seconds + 0.02); sync(p, t0 + seconds + 0.02, False)

    def test_short_move_back_after_a_long_move_is_a_correction(self):
        p = pad()
        self.drag(p, 1, 500, 1000, 500 + 30 * RES, 1000, 100.0, 0.2)      # 30 mm right at 150 mm/s
        self.drag(p, 2, 1500, 1000, 1500 - 2 * RES, 1000, 100.5, 0.3)     # 2 mm back, 0.28 s later
        self.assertEqual([r['kind'] for r in p.sessions], ['move', 'move'])
        self.assertEqual(p.sessions[1]['flag'], 'correction')
        self.assertAlmostEqual(p.sessions[1]['ref'], p.sessions[0]['peak'], places=3)
        self.assertEqual(p.counts['corrections'], 1)
        self.assertEqual(p.counts['longMoves'], 1)
        self.assertAlmostEqual(p.sessions[0]['dx'], 30.0, places=1)

    def test_long_move_continued_at_once_is_a_restroke(self):
        p = pad()
        self.drag(p, 1, 200, 1000, 200 + 25 * RES, 1000, 100.0, 0.25)
        self.drag(p, 2, 200, 1000, 200 + 25 * RES, 1000, 100.6, 0.25)
        self.assertEqual(p.sessions[1]['flag'], 'restroke')
        self.assertEqual(p.counts['restrokes'], 1)
        self.assertEqual(p.counts['longMoves'], 2)

    def test_unrelated_moves_carry_no_flag(self):
        p = pad()
        self.drag(p, 1, 200, 1000, 200 + 25 * RES, 1000, 100.0, 0.25)
        self.drag(p, 2, 200, 1000, 200 - 25 * RES, 1000, 102.0, 0.25)     # 1.7 s later: a new intention
        self.assertEqual(p.sessions[1]['flag'], '')
        self.drag(p, 3, 200, 1000, 200 + 25 * RES, 1000, 103.0, 0.25)
        self.drag(p, 4, 1500, 1000, 1500 + 2 * RES, 1000, 103.4, 0.3)     # short, same direction: not a correction
        self.assertEqual(p.sessions[3]['flag'], '')

    def test_scroll_reversal_and_scroll_restroke(self):
        p = pad()
        def scroll(tracking, y0, y1, t0):
            a, b = Finger(p, 0, tracking), Finger(p, 1, tracking + 50)
            a.down(1000, y0, t0); b.down(1000 + 15 * RES, y0, t0); sync(p, t0, True)
            for i in range(1, 6):
                t = t0 + 0.05 * i
                a.move(1000, int(y0 + (y1 - y0) * i / 5), t); b.move(1000 + 15 * RES, int(y0 + (y1 - y0) * i / 5), t); sync(p, t)
            a.up(t0 + 0.3); b.up(t0 + 0.3); sync(p, t0 + 0.3, False)
        scroll(1, 500, 500 + 20 * RES, 100.0)
        scroll(2, 1200, 1200 - 5 * RES, 100.5)
        self.assertEqual([r['kind'] for r in p.sessions], ['scroll', 'scroll'])
        self.assertEqual(p.sessions[1]['flag'], 'scrollCorrection')
        scroll(3, 500, 500 + 20 * RES, 102.0)
        scroll(4, 500, 500 + 20 * RES, 102.4)
        self.assertEqual(p.sessions[3]['flag'], 'scrollRestroke')
        self.assertEqual(p.counts['scrollCorrections'], 1)
        self.assertEqual(p.counts['scrollRestrokes'], 1)


def synthetic_sessions(n_moves=400, correction_after_fast=0.0, restrokes=0.0, slow_corrections=0.0, scrolls=0, reversals=0.0):
    """A window of sessions with tunable fingerprint rates, speeds split around 50 mm/s."""
    out, t = [], 1_000_000.0
    for i in range(n_moves):
        fast = i % 2 == 0
        peak = 120.0 if fast else 20.0
        out.append({'ts': t, 'kind': 'move', 'fingers': 1, 'duration': 0.3, 'dist': 25.0, 'peak': peak, 'mean': peak * 0.6, 'dx': 25.0, 'dy': 0.0, 'flag': '', 'ref': 0.0})
        t += 1
        if fast and (i // 2) < int(n_moves / 2 * correction_after_fast):
            out.append({'ts': t, 'kind': 'move', 'fingers': 1, 'duration': 0.1, 'dist': 2.0, 'peak': 30.0, 'mean': 20.0, 'dx': -2.0, 'dy': 0.0, 'flag': 'correction', 'ref': peak})
            t += 1
        if not fast and (i // 2) < int(n_moves / 2 * slow_corrections):
            out.append({'ts': t, 'kind': 'move', 'fingers': 1, 'duration': 0.1, 'dist': 2.0, 'peak': 15.0, 'mean': 10.0, 'dx': -2.0, 'dy': 0.0, 'flag': 'correction', 'ref': peak})
            t += 1
        if fast and (i // 2) < int(n_moves / 2 * restrokes):
            out.append({'ts': t, 'kind': 'move', 'fingers': 1, 'duration': 0.3, 'dist': 25.0, 'peak': peak, 'mean': 70.0, 'dx': 25.0, 'dy': 0.0, 'flag': 'restroke', 'ref': peak})
            t += 1
    for i in range(scrolls):
        out.append({'ts': t, 'kind': 'scroll', 'fingers': 2, 'duration': 0.3, 'dist': 20.0, 'peak': 80.0, 'mean': 60.0, 'dx': 0.0, 'dy': 20.0,
                    'flag': 'scrollCorrection' if i < int(scrolls * reversals) else '', 'ref': 80.0})
        t += 1
    return out


def synthetic_hist(median_mm=15.0, p90_mm=90.0, seconds=1800.0):
    """Movement time spread so the given percentiles fall where asked."""
    hist = [0.0] * (tp.BINS + 1)
    m, n = int(median_mm / tp.BIN_MM_S), int(p90_mm / tp.BIN_MM_S)
    for i in range(m):
        hist[i] = seconds * 0.5 / m
    for i in range(m, n):
        hist[i] = seconds * 0.4 / max(1, n - m)
    hist[n] = seconds * 0.1
    return hist


class OptimizerTests(unittest.TestCase):
    CURRENT = {'profile': 'custom', 'curve': {'precision': 0.1875, 'start': 1.3, 'end': 2.6, 'fast': 0.6225}, 'scrollFactor': 0.33, 'scrollScale': 1.0, 'gainMaximum': 1.0}

    def test_fit_places_start_and_end_at_the_percentiles(self):
        out = tp.propose(self.CURRENT, synthetic_sessions(), synthetic_hist(15, 90))
        keys = {c['key']: c for c in out['changes']}
        self.assertIn('start', keys)
        self.assertIn('end', keys)
        self.assertAlmostEqual(out['proposal']['curve']['start'], round(tp.percentile(synthetic_hist(15, 90), 0.45) / 25.4, 2), places=2)
        self.assertAlmostEqual(out['proposal']['curve']['end'], round(90 / 25.4, 2), places=2)
        self.assertEqual(out['verdict'], 'fit')
        self.assertEqual(out['confidence'], 'medium')
        self.assertEqual(out['proposal']['curve']['fast'], self.CURRENT['curve']['fast'], 'no fingerprint, no gain change')

    def test_overshoots_after_fast_moves_lower_fast_gain_by_one_nudge(self):
        out = tp.propose(self.CURRENT, synthetic_sessions(correction_after_fast=0.4), synthetic_hist(15, 90))
        fast = [c for c in out['changes'] if c['key'] == 'fast']
        self.assertEqual(len(fast), 1)
        self.assertAlmostEqual(fast[0]['to'], round(0.6225 * tp.NUDGE_DOWN, 4))
        self.assertEqual(out['verdict'], 'nudge')

    def test_restrokes_raise_fast_gain_unless_at_the_ceiling(self):
        out = tp.propose(self.CURRENT, synthetic_sessions(restrokes=0.3), synthetic_hist(15, 90))
        fast = [c for c in out['changes'] if c['key'] == 'fast']
        self.assertEqual(len(fast), 1)
        self.assertAlmostEqual(fast[0]['to'], round(0.6225 * tp.NUDGE_UP, 4))
        capped = dict(self.CURRENT, curve=dict(self.CURRENT['curve'], fast=0.95))
        out = tp.propose(capped, synthetic_sessions(restrokes=0.3), synthetic_hist(15, 90))
        self.assertFalse([c for c in out['changes'] if c['key'] == 'fast'])
        self.assertTrue(any('Device scale' in n for n in out['notes']))

    def test_both_signals_cancel_and_say_so(self):
        out = tp.propose(self.CURRENT, synthetic_sessions(correction_after_fast=0.4, restrokes=0.3), synthetic_hist(15, 90))
        self.assertFalse([c for c in out['changes'] if c['key'] == 'fast'])
        self.assertTrue(any('cancel' in n for n in out['notes']))

    def test_slow_corrections_lower_precision_and_scroll_reversals_lower_scroll(self):
        out = tp.propose(self.CURRENT, synthetic_sessions(slow_corrections=0.5, scrolls=60, reversals=0.5), synthetic_hist(15, 90))
        keys = {c['key']: c for c in out['changes']}
        self.assertAlmostEqual(keys['precision']['to'], round(0.1875 * tp.NUDGE_DOWN, 4))
        self.assertAlmostEqual(keys['scroll']['to'], round(0.33 * tp.NUDGE_DOWN, 2))

    def test_system_profile_gets_a_first_fit_from_the_preset(self):
        current = dict(self.CURRENT, profile='adaptive', curve=None)
        out = tp.propose(current, synthetic_sessions(), synthetic_hist(15, 90))
        self.assertEqual(out['verdict'], 'first fit')
        self.assertEqual(out['proposal']['profile'], 'custom')
        self.assertLessEqual(out['proposal']['curve']['fast'], 1.0)
        self.assertGreater(out['proposal']['curve']['end'], out['proposal']['curve']['start'])

    def test_thin_data_fits_the_shape_but_withholds_the_gains(self):
        out = tp.propose(self.CURRENT, synthetic_sessions(n_moves=40, correction_after_fast=0.9), synthetic_hist(15, 90, seconds=60))
        self.assertEqual(out['confidence'], 'low')
        self.assertFalse([c for c in out['changes'] if c['key'] == 'fast'])
        self.assertIn('Fewer than five minutes', out['message'])

    def test_previous_pass_is_judged_by_rates_since_it_was_applied(self):
        sessions = synthetic_sessions(correction_after_fast=0.4)
        log = [{'ts': sessions[len(sessions) // 2]['ts'], 'applied': True, 'changes': [{'key': 'fast'}], 'evidence': {'fastCorrectionRate': 0.4}, 'practiceMedianMs': 900}]
        out = tp.propose(dict(self.CURRENT, practiceMedianMs=700), sessions, synthetic_hist(15, 90), log)
        self.assertIsNotNone(out['previous'])
        self.assertEqual(out['previous']['before']['fastCorrectionRate'], 0.4)
        self.assertIn('fastCorrectionRate', out['previous']['after'])
        self.assertEqual(out['previous']['practiceAfter'], 700)

    def test_log_round_trip_and_sessions_table(self):
        with tempfile.TemporaryDirectory() as directory:
            tp.STATE = Path(directory)
            self.assertEqual(tp.load_log(), [])
            tp.optimize_applied({'changes': [{'key': 'start'}], 'evidence': {'correctionRate': 0.1}, 'practiceMedianMs': 800})
            self.assertEqual(len(tp.load_log()), 1)
            db = tp.db_open()
            recs = [{'wall': 5.0, 'start': 0, 'end': 0.3, 'kind': 'move', 'fingers': 1, 'duration': 0.3, 'dist': 20.0, 'peak': 100.0, 'mean': 66.0, 'dx': 20.0, 'dy': 0.0, 'flag': '', 'ref': 0.0}]
            tp.record_sessions(db, recs)
            rows = tp.load_sessions(db, 0)
            self.assertEqual(rows[0]['kind'], 'move')
            self.assertEqual(rows[0]['dist'], 20.0)
            out = tp.optimize({'profile': 'custom', 'curve': self.CURRENT['curve'], 'scrollFactor': 0.33, 'gainMaximum': 1})
            self.assertIn('verdict', out)
            self.assertIsNotNone(out['previous'])


class DataFeatureTests(unittest.TestCase):
    def test_heat_and_hours_accumulate_per_finger_frame(self):
        p = pad()
        f = Finger(p, 0, 1)
        f.down(4010 // 2, 2468 // 2, 100.0); sync(p, 100.0, True)
        f.move(4010 // 2 + 3, 2468 // 2, 100.05); sync(p, 100.05)
        f.up(100.1); sync(p, 100.1, False)
        heat, hours, palm = p.take_maps()
        self.assertEqual(len(heat), tp.HEAT_W * tp.HEAT_H)
        self.assertEqual(sum(heat), 2, 'one cell per active finger per frame')
        self.assertEqual(heat[(tp.HEAT_H // 2) * tp.HEAT_W + tp.HEAT_W // 2], 2)
        self.assertEqual(sum(hours), 1, 'one session, one hour bucket')
        self.assertEqual(sum(p.take_maps()[0]), 0, "taken maps reset")
        self.assertEqual(palm, (0.0, 0.0, 0))

    def test_days_table_and_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            tp.STATE = Path(directory)
            db = tp.db_open()
            now = 1_700_000_000.0
            today = tp.Recorder._fresh_today(None, now)
            today['counts']['distance'] = 500.0; today['counts']['touches'] = 5; today['counts']['active'] = 30.0
            for back in (1, 2, 10, 40):
                past = tp.Recorder._fresh_today(None, now - back * 86400)
                past['counts']['distance'] = 1000.0 * back; past['counts']['touches'] = 10 * back; past['counts']['active'] = 60.0
                tp.record_day(db, past)
            tp.record_day(db, today)
            for minute in range(3):
                tp.record(db, now - 600 - minute * 60, dict(tp.zero_counters(), touches=1, distance=100.0, active=5.0), [0.0] * (tp.BINS + 1), 'evdev')
            recent = [(now - 65, dict(tp.zero_counters(), touches=9)), (now - 30, dict(tp.zero_counters(), touches=2, clicks=1, distance=40.0)), (now - 5, dict(tp.zero_counters(), taps=1))]
            w = tp.windows(db, today, now, recent)
            self.assertEqual(w['minute'], {'distance': 40.0, 'touches': 2, 'taps': 1, 'clicks': 1, 'active': 0.0}, 'the trailing minute drops the 65 s old bucket')
            self.assertEqual(w['year']['distance'], w['all']['distance'])
            self.assertEqual(w['hour']['distance'], 300.0)
            self.assertEqual(w['today']['distance'], 500.0)
            self.assertEqual(w['week']['distance'], 500.0 + 1000.0 + 2000.0)
            self.assertEqual(w['month']['distance'], 500.0 + 1000.0 + 2000.0 + 10000.0)
            self.assertEqual(w['all']['distance'], 500.0 + 1000.0 + 2000.0 + 10000.0 + 40000.0)
            self.assertEqual(w['all']['days'], 5)
            self.assertEqual(w['firstDay'], tp.day_key(now - 40 * 86400))


class GestureTests(unittest.TestCase):
    def test_pair_actions_own_their_axis_and_unknowns_are_refused(self):
        clean = tp.normalize_gestures({'3-left': 'ws-slide', '4-up': 'bg-next'})
        self.assertEqual(clean['3-right'], 'ws-slide', 'the partner direction mirrors a pair action')
        self.assertEqual(clean['4-up'], 'bg-next')
        self.assertEqual(clean['4-down'], 'none')
        with self.assertRaises(RuntimeError):
            tp.normalize_gestures({'5-left': 'ws-slide'})
        with self.assertRaises(RuntimeError):
            tp.normalize_gestures({'3-left': 'rm -rf /'})
        with self.assertRaises(RuntimeError):
            tp.normalize_gestures(['3-left'])

    def test_lua_emits_each_axis_once_and_quotes_commands(self):
        lua = tp.gestures_lua(tp.normalize_gestures({'3-left': 'ws-slide', '3-right': 'ws-slide', '4-up': 'bg-next', '3-down': 'ws-scratch', '4-pinchout': 'zoom'}))
        self.assertEqual(lua.count('direction = "horizontal", action = "workspace"'), 1)
        self.assertNotIn('direction = "left"', lua)
        self.assertIn('hl.gesture({ fingers = 4, direction = "up", action = function() hl.exec_cmd("omarchy-theme-bg-next") end })', lua)
        self.assertIn('action = "special", workspace_name = "scratchpad"', lua)
        self.assertIn('action = "cursor_zoom", zoom_level = 2', lua)
        self.assertTrue(lua.startswith('do -- Managed by nixfred.trackpad-pulse'))
        self.assertTrue(lua.endswith('end\n'))
        self.assertEqual(tp.gestures_lua(tp.normalize_gestures({})).count('hl.gesture'), 0)
        self.assertEqual(tp.lua_quote('say "hi" \\ there'), '"say \\"hi\\" \\\\ there"')

    def test_every_catalogue_action_renders(self):
        for action in tp.CATALOGUE:
            lua = tp.gestures_lua(tp.normalize_gestures({'3-up': action['id']}))
            if action['id'] != 'none':
                self.assertIn('hl.gesture({ fingers = 3', lua, action['id'])
        catalogue = tp.gesture_catalogue()
        self.assertEqual(len(catalogue['actions']), len(tp.CATALOGUE))
        self.assertEqual(set(catalogue['defaults']), set(tp.SLOTS))
        self.assertTrue(all(a['group'] and a['label'] and a['hint'] for a in catalogue['actions']))

    def test_theme_step_is_cyclic_and_random_avoids_current(self):
        names = ['A', 'B', 'C']
        self.assertEqual(tp.pick_theme(names, 'C', 'next'), 'A')
        self.assertEqual(tp.pick_theme(names, 'A', 'prev'), 'C')
        self.assertEqual(tp.pick_theme(names, 'Unknown', 'next'), 'A')
        for _ in range(20):
            self.assertNotEqual(tp.pick_theme(names, 'B', 'random'), 'B')
        with self.assertRaises(RuntimeError):
            tp.pick_theme([], 'A', 'next')

    def test_hint_signature_depends_only_on_keys_and_targets(self):
        a = tp.hint_signature([{'key': 'start', 'to': 0.59, 'from': 0.8, 'reason': 'x'}, {'key': 'end', 'to': 3.15}])
        b = tp.hint_signature([{'key': 'end', 'to': 3.15, 'reason': 'y'}, {'key': 'start', 'to': 0.59}])
        self.assertEqual(a, b)
        self.assertNotEqual(a, tp.hint_signature([{'key': 'start', 'to': 0.6}]))


class HandMouseReportTests(unittest.TestCase):
    def test_hand_verdict_reads_thumb_and_palm_sides(self):
        heat = [0] * (tp.HEAT_W * tp.HEAT_H)
        for row in range(int(tp.HEAT_H * 0.6), tp.HEAT_H):
            for col in range(0, int(tp.HEAT_W * 0.3)):
                heat[row * tp.HEAT_W + col] = 20          # thumb parked bottom-left
        right = tp.hand_verdict(heat, palm_x=0.85 * 12, palm_n=12)
        self.assertEqual(right['hand'], 'right')
        self.assertGreater(right['confidence'], 0.5)
        mirrored = heat[::-1]
        left = tp.hand_verdict(mirrored, palm_x=0.15 * 12, palm_n=12)
        self.assertEqual(left['hand'], 'left')
        self.assertEqual(tp.hand_verdict([0] * (tp.HEAT_W * tp.HEAT_H), 0.0, 0)['hand'], 'unknown')

    def test_mouse_watch_attributes_only_untouched_motion_and_tracks_streaks(self):
        m = tp.MouseWatch()
        self.assertFalse(m.observe(100, 100, 10.0, False))
        self.assertTrue(m.observe(110, 100, 10.2, False))
        self.assertFalse(m.observe(120, 100, 10.4, True), 'motion while a finger is down is the pad, not a mouse')
        for i in range(1, 80):
            m.observe(120 + i, 100, 10.4 + i * 0.2, False)
        self.assertGreater(m.streak, 15.0)
        self.assertAlmostEqual(m.take()['distance'], 10.0 + 79.0, places=6)
        m.observe(300, 100, 40.0, False)   # 13 s of silence ends the streak
        self.assertLess(m.streak, 1.0)

    def test_report_totals_days_apps_and_optimize_history(self):
        with tempfile.TemporaryDirectory() as directory:
            tp.STATE = Path(directory)
            db = tp.db_open()
            now = 1_700_000_000.0
            for back in range(1, 10):
                past = tp.Recorder._fresh_today(None, now - back * 86400)
                past['counts']['touches'] = 100; past['counts']['distance'] = 1000.0; past['counts']['clicks'] = 5
                past['apps'] = {'brave': {'touches': 60, 'distance': 600.0, 'active': 30.0}, 'kitty': {'touches': 40, 'distance': 400.0, 'active': 20.0}}
                past['mouse'] = {'active': 120.0, 'distance': 5000.0}
                tp.record_day(db, past)
            today = tp.Recorder._fresh_today(None, now)
            today['counts']['touches'] = 7; today['apps'] = {'kitty': {'touches': 7, 'distance': 70.0, 'active': 3.0}}
            log = [{'ts': now - 3 * 86400, 'applied': True, 'changes': [{'key': 'start', 'label': 'Start'}], 'evidence': {'correctionRate': 0.2}, 'verdict': 'fit'}]
            r = tp.report(db, today, log, now)
            self.assertEqual(r['week']['days'], 7)
            self.assertEqual(r['week']['touches'], 607)
            self.assertEqual(r['lastWeek']['touches'], 300)
            self.assertEqual(r['apps'][0]['app'], 'brave')
            self.assertEqual(r['apps'][1]['touches'], 247)
            self.assertEqual(r['week']['mouse'], 720.0)
            self.assertEqual(r['optimize'][0]['changes'], ['Start'])
            self.assertEqual(r['days'][-1]['day'], today['day'])


if __name__ == '__main__':
    unittest.main()
