import QtQuick
import "Pulse.js" as Pulse

// Where the fingers spend their time: seconds of movement per speed bin.
// Optionally shades the band where the active pointer curve accelerates, so
// the shape of the curve can be judged against the hand that drives it.
Item {
    id: root
    property var hist: []
    property real binWidth: 5
    property real mmPerUnitMs: 25.4
    // Curve start/end in the editor's 0..4 units, or -1 for none.
    property real curveStart: -1
    property real curveEnd: -1
    property bool cursorUnits: false
    property color tint: '#43f2a1'
    property color heat: '#ffa86b'
    property color ink: '#edf5f7'
    property color surface: '#17232d'
    readonly property color axis: Qt.alpha(ink, 0.58)
    readonly property int gutter: 8
    readonly property int foot: 22
    readonly property real total: Pulse.total(hist)
    readonly property real median: Pulse.percentile(hist, binWidth, 0.5)
    readonly property real p90: Pulse.percentile(hist, binWidth, 0.9)
    // The unit multiplier for labels: the cursor fallback records px/s in
    // bins ten times wider.
    readonly property real unit: cursorUnits ? 10 : 1
    property int hoverIndex: -1
    function repaint() { if (root.visible) canvas.requestPaint() }
    onHistChanged: { hoverIndex = -1; repaint() }
    onTintChanged: repaint()
    onCurveStartChanged: repaint()
    onCurveEndChanged: repaint()
    onVisibleChanged: repaint()
    function plotWidth() { return Math.max(1, width - gutter * 2) }
    function xFor(mm) { return gutter + plotWidth() * Pulse.clamp(mm / (binWidth * hist.length), 0, 1) }
    Canvas {
        id: canvas
        anchors.fill: parent
        onWidthChanged: root.repaint()
        onHeightChanged: root.repaint()
        onPaint: {
            var c = getContext('2d'), w = root.plotWidth(), h = height - root.foot, x0 = root.gutter
            c.reset(); c.clearRect(0, 0, width, height)
            var bins = root.hist || [], n = Math.max(1, bins.length), bw = w / n
            var peak = 0
            for (var i = 0; i < bins.length; i++) peak = Math.max(peak, Pulse.num(bins[i]))
            // The acceleration band, in the same axis.
            if (root.curveStart >= 0 && root.curveEnd > root.curveStart) {
                var s = root.xFor(Pulse.curveToMm(root.curveStart, root.mmPerUnitMs)), e = root.xFor(Pulse.curveToMm(root.curveEnd, root.mmPerUnitMs))
                c.fillStyle = Qt.alpha(root.heat, 0.10); c.fillRect(s, 6, e - s, h - 6)
                c.strokeStyle = Qt.alpha(root.heat, 0.55); c.lineWidth = 1
                c.beginPath(); c.moveTo(s, 6); c.lineTo(s, h); c.moveTo(e, 6); c.lineTo(e, h); c.stroke()
                var top = root.xFor(Pulse.curveToMm(4, root.mmPerUnitMs))
                c.strokeStyle = Qt.alpha(root.ink, 0.25)
                c.setLineDash([3, 3]); c.beginPath(); c.moveTo(top, 6); c.lineTo(top, h); c.stroke(); c.setLineDash([])
            }
            for (var k = 0; k < bins.length; k++) {
                var v = Pulse.num(bins[k]), bh = peak > 0 ? (h - 8) * v / peak : 0, hot = k === root.hoverIndex
                c.fillStyle = hot ? Qt.lighter(root.tint, 1.3) : Qt.alpha(root.tint, k === bins.length - 1 ? 0.45 : 0.78)
                c.fillRect(x0 + k * bw + 0.5, h - bh, Math.max(1, bw - 1), bh)
            }
            c.strokeStyle = Qt.alpha(root.ink, 0.3); c.lineWidth = 1
            c.beginPath(); c.moveTo(x0, h + 0.5); c.lineTo(x0 + w, h + 0.5); c.stroke()
            if (root.total > 0) {
                c.strokeStyle = root.ink; c.lineWidth = 1.2
                var mx = root.xFor(root.median), px = root.xFor(root.p90)
                c.beginPath(); c.moveTo(mx, 4); c.lineTo(mx, h); c.stroke()
                c.setLineDash([2, 3]); c.beginPath(); c.moveTo(px, 4); c.lineTo(px, h); c.stroke(); c.setLineDash([])
            }
            c.fillStyle = root.axis; c.font = '10px sans-serif'
            var labels = root.cursorUnits ? [0, 500, 1000, 1500] : [0, 50, 100, 150]
            for (var l = 0; l < labels.length; l++) {
                c.textAlign = l === 0 ? 'left' : l === labels.length - 1 ? 'right' : 'center'
                c.fillText(String(labels[l]) + (l === labels.length - 1 ? (root.cursorUnits ? '+ px/s' : '+ mm/s') : ''), root.xFor(labels[l] / root.unit), height - 3)
            }
        }
    }
    Rectangle {
        visible: root.hoverIndex >= 0 && root.hoverIndex < (root.hist || []).length
        anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter
        width: hoverText.implicitWidth + 20; height: 27; radius: 7; color: root.surface; border.color: Qt.alpha(root.ink, 0.32)
        Text {
            id: hoverText; anchors.centerIn: parent; color: root.ink; font.pixelSize: 11; textFormat: Text.PlainText
            text: {
                var i = root.hoverIndex
                if (i < 0 || i >= (root.hist || []).length) return ''
                var lo = i * root.binWidth * root.unit, hi = (i + 1) * root.binWidth * root.unit
                var share = root.total > 0 ? Pulse.num(root.hist[i]) / root.total : 0
                return (i === root.hist.length - 1 ? 'over ' + lo : lo + ' – ' + hi) + (root.cursorUnits ? ' px/s' : ' mm/s') + '  ·  ' + Pulse.duration(root.hist[i]) + '  ·  ' + (share * 100).toFixed(1) + '% of movement'
            }
        }
    }
    MouseArea {
        anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton
        onExited: root.hoverIndex = -1
        onPositionChanged: function(mouse) {
            var n = (root.hist || []).length
            root.hoverIndex = n > 0 ? Math.floor((mouse.x - root.gutter) / root.plotWidth() * n) : -1
            if (root.hoverIndex >= n || mouse.x < root.gutter) root.hoverIndex = -1
        }
    }
}
