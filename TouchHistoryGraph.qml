import QtQuick
import "Pulse.js" as Pulse

// Touches per bucket as bars, peak finger speed as a line on the right axis.
// Points are [bucketStart, touches, distance mm, peak mm/s, active s, taps,
// boot]. A bucket that was never recorded is absent, so a gap in the bars is
// a gap in the recording and not a quiet hour; a reboot breaks the line.
Item {
    id: root
    property bool axesVisible: true
    property var historyData: ({points: [], seconds: 3600, now: 0, bucket: 60, busiest: 0, peak: 0})
    property color tint: '#43f2a1'
    property color heat: '#ffa86b'
    property color ink: '#edf5f7'
    property color surface: '#17232d'
    readonly property color grid: Qt.alpha(ink, 0.17)
    readonly property color axis: Qt.alpha(ink, 0.58)
    property int hoverIndex: -1
    readonly property var points: historyData && historyData.points ? historyData.points : []
    readonly property var hoverPoint: hoverIndex >= 0 && hoverIndex < points.length ? points[hoverIndex] : null
    readonly property real ceiling: Math.max(1, Pulse.num(historyData.busiest))
    readonly property real speedCeiling: Math.max(50, Math.ceil(Pulse.num(historyData.peak) / 50) * 50)
    readonly property int gutter: axesVisible ? 40 : 0
    readonly property int gutterRight: axesVisible ? 46 : 0
    function repaint() { if (root.visible) graph.requestPaint() }
    onHistoryDataChanged: { hoverIndex = -1; repaint() }
    onTintChanged: repaint()
    onHeatChanged: repaint()
    onInkChanged: repaint()
    onVisibleChanged: repaint()
    function plotWidth() { return Math.max(1, width - gutter - gutterRight) }
    function xAt(ts) { return gutter + plotWidth() * (ts - (historyData.now - historyData.seconds)) / Math.max(1, historyData.seconds) }
    function bucketLabel(ts) {
        var b = Math.max(60, Pulse.num(historyData.bucket)), from = new Date(ts * 1000)
        if (b < 3600) return Qt.formatDateTime(from, 'h:mm AP')
        if (b < 86400) return Qt.formatDateTime(from, 'ddd h AP') + ' – ' + Qt.formatDateTime(new Date((ts + b) * 1000), 'h AP')
        return Qt.formatDateTime(from, 'ddd d MMM')
    }
    Canvas {
        id: graph
        anchors.fill: parent
        onWidthChanged: root.repaint()
        onHeightChanged: root.repaint()
        Connections { target: root; function onAxesVisibleChanged() { root.repaint() } }
        onPaint: {
            var foot = root.axesVisible ? 26 : 4
            var c = getContext('2d'), w = root.plotWidth(), h = height - foot, x0 = root.gutter, base = h
            c.reset(); c.clearRect(0, 0, width, height)
            c.font = '10px sans-serif'
            for (var line = 0; line <= 4; line++) {
                var y = 8 + (h - 8) * line / 4
                c.strokeStyle = root.grid; c.lineWidth = 1
                c.beginPath(); c.moveTo(x0, y); c.lineTo(x0 + w, y); c.stroke()
                if (root.axesVisible) {
                    c.fillStyle = root.axis; c.textAlign = 'right'
                    c.fillText(String(Math.round(root.ceiling * (1 - line / 4))), x0 - 5, y + 3)
                    c.textAlign = 'left'
                    c.fillText(String(Math.round(root.speedCeiling * (1 - line / 4))), x0 + w + 5, y + 3)
                }
            }
            function yTouch(v) { return 8 + (h - 8) * (1 - Pulse.clamp(v, 0, root.ceiling) / root.ceiling) }
            function ySpeed(v) { return 8 + (h - 8) * (1 - Pulse.clamp(v, 0, root.speedCeiling) / root.speedCeiling) }
            var pts = root.points, bucket = Math.max(60, Pulse.num(root.historyData.bucket))
            var bw = Math.max(1, w * bucket / Math.max(1, root.historyData.seconds) - 1)
            for (var i = 0; i < pts.length; i++) {
                var p = pts[i], x = root.xAt(p[0]), hot = i === root.hoverIndex, top = yTouch(p[1])
                c.fillStyle = hot ? Qt.lighter(root.tint, 1.25) : Qt.alpha(root.tint, 0.75)
                c.fillRect(x, top, bw, Math.max(p[1] > 0 ? 1 : 0, base - top))
            }
            c.strokeStyle = root.heat; c.lineWidth = 1.6
            c.beginPath()
            for (var k = 0; k < pts.length; k++) {
                var q = pts[k], qx = root.xAt(q[0]) + bw / 2, qy = ySpeed(q[3])
                if (k === 0 || q[0] - pts[k - 1][0] > bucket * 2.5 || q[6] !== pts[k - 1][6]) c.moveTo(qx, qy); else c.lineTo(qx, qy)
            }
            c.stroke()
            c.strokeStyle = Qt.alpha(root.ink, 0.3); c.lineWidth = 1
            c.beginPath(); c.moveTo(x0, base + 0.5); c.lineTo(x0 + w, base + 0.5); c.stroke()
            if (root.axesVisible) {
                c.fillStyle = root.axis; c.textAlign = 'left'
                c.fillText(root.historyData.seconds === 3600 ? '1 hour ago' : root.historyData.seconds === 86400 ? '24 hours ago' : '7 days ago', x0, height - 3)
                c.textAlign = 'right'; c.fillText('now', x0 + w, height - 3)
            }
        }
    }
    Rectangle {
        visible: root.hoverPoint !== null
        anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter
        width: hoverText.implicitWidth + 20; height: 27; radius: 7; color: root.surface; border.color: Qt.alpha(root.ink, 0.32)
        Text {
            id: hoverText; anchors.centerIn: parent; color: root.ink; font.pixelSize: 11; textFormat: Text.PlainText
            text: {
                var p = root.hoverPoint
                if (!p) return ''
                return root.bucketLabel(p[0]) + '  ·  ' + p[1] + ' touches  ·  ' + p[5] + ' taps  ·  ' + Pulse.distance(p[2]) + '  ·  peak ' + Pulse.speed(p[3]) + '  ·  ' + Pulse.duration(p[4]) + ' active'
            }
        }
    }
    MouseArea {
        anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton
        onExited: root.hoverIndex = -1
        onPositionChanged: function(mouse) {
            var wanted = root.historyData.now - root.historyData.seconds + (mouse.x - root.gutter) / root.plotWidth() * root.historyData.seconds, best = -1
            var bucket = Math.max(60, Pulse.num(root.historyData.bucket))
            for (var i = 0; i < root.points.length; i++) {
                var t = root.points[i][0]
                if (wanted >= t && wanted < t + bucket) { best = i; break }
            }
            root.hoverIndex = best
        }
    }
}
