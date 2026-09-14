// Formatting and readout helpers for the Trackpad Pulse dashboard. Pure
// functions over the collector's snapshot / live / history JSON, so they run
// unchanged under node for the tests.

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, Number(v) || 0)) }

function num(v) { return Number(v) || 0 }

function int(v) {
    var n = Math.round(num(v))
    return n.toLocaleString ? n.toLocaleString() : String(n)
}

function distance(mm) {
    var v = num(mm)
    if (v < 1000) return v.toFixed(v < 10 ? 1 : 0) + ' mm'
    if (v < 1000000) return (v / 1000).toFixed(v < 100000 ? 1 : 0) + ' m'
    return (v / 1000000).toFixed(2) + ' km'
}

function speed(mmPerS) {
    var v = num(mmPerS)
    return v >= 1000 ? (v / 1000).toFixed(2) + ' m/s' : v.toFixed(0) + ' mm/s'
}

function pxSpeed(pxPerS) {
    var v = num(pxPerS)
    return v >= 10000 ? (v / 1000).toFixed(1) + 'k px/s' : v.toFixed(0) + ' px/s'
}

function duration(seconds) {
    var s = Math.max(0, num(seconds))
    if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + ' s'
    if (s < 3600) return Math.round(s / 60) + ' min'
    var h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60)
    return h + ' h ' + (m < 10 ? '0' : '') + m + ' min'
}

function ago(ts, now) {
    if (!ts) return 'never'
    var s = Math.max(0, num(now) - num(ts))
    if (s < 5) return 'just now'
    if (s < 60) return Math.round(s) + ' s ago'
    if (s < 3600) return Math.round(s / 60) + ' min ago'
    if (s < 86400) return (s / 3600).toFixed(1) + ' h ago'
    return (s / 86400).toFixed(1) + ' d ago'
}

function clock(ts) {
    if (!ts) return '—'
    return Qt.formatTime(new Date(num(ts) * 1000), 'h:mm AP')
}

function pct(v) { return (num(v) * 100).toFixed(0) + '%' }

function hz(v) { return num(v) > 0 ? Math.round(num(v)) + ' Hz' : '—' }

// ---- histogram ----------------------------------------------------------
function total(hist) {
    var t = 0
    for (var i = 0; i < (hist || []).length; i++) t += num(hist[i])
    return t
}

// Speed below which `fraction` of the moving time was spent, in mm/s.
function percentile(hist, binWidth, fraction) {
    var t = total(hist)
    if (t <= 0) return 0
    var acc = 0
    for (var i = 0; i < hist.length; i++) {
        acc += num(hist[i])
        if (acc / t >= fraction) return (i + 1) * binWidth
    }
    return hist.length * binWidth
}

// Share of moving time spent below a speed, 0..1.
function shareBelow(hist, binWidth, mmPerS) {
    var t = total(hist)
    if (t <= 0) return 0
    var acc = 0
    for (var i = 0; i < hist.length; i++) {
        if ((i + 1) * binWidth <= mmPerS) acc += num(hist[i])
        else if (i * binWidth < mmPerS) acc += num(hist[i]) * (mmPerS - i * binWidth) / binWidth
    }
    return acc / t
}

// The curve editor's x axis in mm/s: libinput's custom profile takes device
// speed in units per millisecond, touchpads are normalized to 1000 dpi, so
// one unit per ms is 25.4 mm/s. A curve position (0..4) times this is mm/s.
function curveToMm(x, mmPerUnitMs) { return num(x) * (num(mmPerUnitMs) || 25.4) }
function mmToCurve(mm, mmPerUnitMs) { return num(mm) / (num(mmPerUnitMs) || 25.4) }

// ---- readouts -----------------------------------------------------------
var MODES = [
    {name: 'Touches today',  tag: 'TOUCHES'},
    {name: 'Distance today', tag: 'TRAVELLED'},
    {name: 'Taps today',     tag: 'TAPS'},
    {name: 'Peak speed',     tag: 'PEAK'},
    {name: 'Fingers now',    tag: 'FINGERS'},
    {name: 'Report rate',    tag: 'RATE'},
    {name: 'Active time',    tag: 'ACTIVE'},
    {name: 'Palms rejected', tag: 'PALMS'}
]

function modeName(i) { return (MODES[i] || MODES[0]).name }
function modeTag(i) { return (MODES[i] || MODES[0]).tag }

function readout(snap, live, mode) {
    if (!snap || !snap.warm) return '—'
    var t = snap.today || {}, c = t.counts || {}
    var cursorOnly = snap.access === 'cursor'
    var fingers = 0, rate = 0
    for (var i = 0; live && live.pads && i < live.pads.length; i++) {
        fingers += (live.pads[i].fingers || []).length
        rate = Math.max(rate, num(live.pads[i].hz))
    }
    if (!rate) for (var j = 0; snap.pads && j < snap.pads.length; j++) rate = Math.max(rate, num(snap.pads[j].hz))
    switch (mode) {
    case 1: return cursorOnly ? int(t.cursor ? t.cursor.distance : 0) + ' px' : distance(c.distance)
    case 2: return int(num(c.taps) + num(c.taps2) + num(c.taps3))
    case 3: return cursorOnly ? pxSpeed(t.cursor ? t.cursor.peak : 0) : speed(t.peak)
    case 4: return cursorOnly ? '—' : String(fingers)
    case 5: return cursorOnly ? '—' : hz(rate)
    case 6: return duration(cursorOnly ? (t.cursor ? t.cursor.active : 0) : c.active)
    case 7: return int(c.palms)
    default: return cursorOnly ? '—' : int(c.touches)
    }
}

// One line that says what the pad is doing right now.
function verdict(snap, live, enabled, connected, now) {
    if (!connected) return 'NO TRACKPAD'
    if (!enabled) return 'TRACKPAD OFF'
    if (!snap || !snap.warm) return 'RECORDER OFFLINE'
    if (now - num(snap.ts) > 20) return 'RECORDER STALE'
    if (snap.access === 'cursor') return 'CURSOR ONLY · NO PAD ACCESS'
    if (snap.access === 'none' || snap.access === 'nopad') return 'NO PAD ACCESS'
    var fingers = 0, speed = 0, palm = false
    for (var i = 0; live && live.pads && i < live.pads.length; i++) {
        var f = live.pads[i].fingers || []
        fingers += f.length
        speed = Math.max(speed, num(live.pads[i].speed))
        for (var k = 0; k < f.length; k++) if (f[k].palm) palm = true
    }
    if (palm) return 'PALM ON THE PAD'
    if (fingers >= 3) return fingers + ' FINGERS · GESTURE'
    if (fingers === 2) return speed > 20 ? 'SCROLLING' : 'TWO FINGERS DOWN'
    if (fingers === 1) return speed > 150 ? 'FAST SWIPE' : speed > 20 ? 'TRACKING' : 'FINGER DOWN'
    var last = num(snap.lastTouch)
    if (last && now - last < 3) return 'JUST LIFTED'
    return 'IDLE · LAST TOUCH ' + ago(last, now).toUpperCase()
}

function accessLabel(snap) {
    if (!snap || !snap.warm) return 'offline'
    switch (snap.access) {
    case 'evdev': return 'reading the touchpad'
    case 'cursor': return 'cursor only'
    case 'nopad': return 'no touchpad found'
    default: return 'no access'
    }
}

// ---- per pad ---------------------------------------------------------------
// The selected pad's preset scale (its size against the screen), 1 when the
// recorder has not measured it.
function presetScale(snap, device) {
    var pads = (snap && snap.pads) || []
    for (var i = 0; i < pads.length; i++)
        if (pads[i].device === device && num(pads[i].presetScale) > 0) return num(pads[i].presetScale)
    return 1
}

// The selected pad's week of finger speeds when more than one pad is known
// and it has history of its own; otherwise the fallback, unchanged.
function padHist(snap, device, fallback) {
    var pads = (snap && snap.pads) || [], seen = {}, count = 0
    for (var i = 0; i < pads.length; i++) if (pads[i].device && !seen[pads[i].device]) { seen[pads[i].device] = true; count++ }
    var own = ((snap && snap.weekByDevice) || {})[device]
    return count > 1 && total(own) > 0 ? own : fallback
}

if (typeof module !== 'undefined') module.exports = {
    presetScale: presetScale, padHist: padHist,
    clamp: clamp, num: num, int: int, distance: distance, speed: speed, pxSpeed: pxSpeed, duration: duration, ago: ago, pct: pct, hz: hz,
    total: total, percentile: percentile, shareBelow: shareBelow, curveToMm: curveToMm, mmToCurve: mmToCurve,
    MODES: MODES, modeName: modeName, modeTag: modeTag, readout: readout, verdict: verdict, accessLabel: accessLabel
}
