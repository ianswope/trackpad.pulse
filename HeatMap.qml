import QtQuick

// Where the fingers land: a coarse grid over the pad, each cell brighter the
// more frames a finger spent there. The outline is the pad at its real aspect,
// so a thumb parked in the bottom-left corner shows up in the bottom-left
// corner.
Item {
    id: root
    property var heat: []
    property int cols: 32
    property int rows: 20
    property real aspect: 1.6
    property color tint: '#43f2a1'
    property color hot: '#ffa86b'
    property color ink: '#edf5f7'
    property color surface: '#0b141b'
    readonly property real peak: {
        var m = 0
        for (var i = 0; i < (heat || []).length; i++) m = Math.max(m, Number(heat[i]) || 0)
        return m
    }
    function repaint() { if (root.visible) canvas.requestPaint() }
    onHeatChanged: repaint()
    onTintChanged: repaint()
    onVisibleChanged: repaint()
    Canvas {
        id: canvas
        anchors.fill: parent
        onWidthChanged: root.repaint()
        onHeightChanged: root.repaint()
        onPaint: {
            var c = getContext('2d'), w = width, h = height
            c.reset(); c.clearRect(0, 0, w, h)
            var pad = 6, bw = w - pad * 2, bh = bw / root.aspect
            if (bh > h - pad * 2) { bh = h - pad * 2; bw = bh * root.aspect }
            var x = (w - bw) / 2, y = (h - bh) / 2, r = 10
            c.beginPath()
            c.moveTo(x + r, y); c.lineTo(x + bw - r, y); c.quadraticCurveTo(x + bw, y, x + bw, y + r)
            c.lineTo(x + bw, y + bh - r); c.quadraticCurveTo(x + bw, y + bh, x + bw - r, y + bh)
            c.lineTo(x + r, y + bh); c.quadraticCurveTo(x, y + bh, x, y + bh - r)
            c.lineTo(x, y + r); c.quadraticCurveTo(x, y, x + r, y); c.closePath()
            c.fillStyle = root.surface; c.fill()
            c.save(); c.clip()
            var cw = bw / root.cols, ch = bh / root.rows, list = root.heat || []
            if (root.peak > 0) {
                for (var i = 0; i < list.length && i < root.cols * root.rows; i++) {
                    var v = (Number(list[i]) || 0) / root.peak
                    if (v <= 0) continue
                    var a = Math.sqrt(v)
                    var col = i % root.cols, row = Math.floor(i / root.cols)
                    c.fillStyle = Qt.alpha(v > 0.6 ? root.hot : root.tint, 0.12 + 0.85 * a)
                    c.fillRect(x + col * cw, y + row * ch, cw + 0.5, ch + 0.5)
                }
            }
            c.restore()
            c.strokeStyle = Qt.alpha(root.tint, 0.8); c.lineWidth = 1.5
            c.stroke()
            if (root.peak <= 0) {
                c.fillStyle = Qt.alpha(root.ink, 0.5); c.font = '11px sans-serif'; c.textAlign = 'center'
                c.fillText('Touch the pad and it lights up here', w / 2, h / 2 + 4)
            }
        }
    }
}
