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


if __name__ == '__main__':
    unittest.main()
