"""Optional Omarchy integration check: the panel's own IpcHandler, isolated offscreen shell.

Trackpad Pulse registers its IPC verbs itself (manageIpc: false) so it can add
status, page, chooser and enable beside the five the base Panel used to
provide. The handler block is lifted verbatim out of Panel.qml and wired to a
stub root, so what is verified is the real handler text. Does not load the
plugin backend or modify desktop settings.
"""
from pathlib import Path
import json, os, re, subprocess, tempfile, time
repo = Path(__file__).resolve().parent
qml = (repo / 'Panel.qml').read_text()
target = re.search(r'^  ipcTarget: "([^"]+)"$', qml, re.M).group(1)
assert re.search(r'^  manageIpc: false$', qml, re.M), 'the panel must own its IPC handler'
start = qml.index('  IpcHandler {')
depth, end = 0, start
for end in range(start, len(qml)):
    if qml[end] == '{':
        depth += 1
    elif qml[end] == '}':
        depth -= 1
        if depth == 0:
            break
handler = qml[start:end + 1].replace('target: root.ipcTarget', 'target: "' + target + '"')
with tempfile.TemporaryDirectory(prefix='trackpad-ipc-') as directory:
    root = Path(directory)
    (root / 'shell.qml').write_text('''import QtQuick
import Quickshell
import Quickshell.Io
ShellRoot {
  QtObject {
    id: root
    property bool opened: false
    property bool chooseMode: false
    property string active: "overview"
    property bool enabled: true
    property var pages: [{key: "overview"}, {key: "controls"}, {key: "feel"}, {key: "lab"}, {key: "about"}]
    property string actionStatus: ""
    function open() { opened = true }
    function close() { opened = false }
    function toggle() { opened = !opened }
    function status() { return JSON.stringify({opened: opened, active: active, chooseMode: chooseMode, enabled: enabled}) }
    function showPage(key) { for (var i = 0; i < pages.length; i++) if (pages[i].key === key) { chooseMode = false; active = key; return true } actionStatus = "No such page: " + key; return false }
    function setTouchpadEnabled(on) { enabled = on }
    property bool optimized: false
    function requestOptimize() { optimized = true; active = "feel" }
  }
''' + handler + '''
  IpcHandler {
    target: "verification"
    function opened(): bool { return root.opened }
    function active(): string { return root.active }
    function chooseMode(): bool { return root.chooseMode }
    function enabled(): bool { return root.enabled }
    function optimized(): bool { return root.optimized }
  }
}
''')
    (root / 'runtime').mkdir(mode=0o700)
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QPA_PLATFORMTHEME='basic', QT_QUICK_CONTROLS_STYLE='Basic', XDG_RUNTIME_DIR=str(root / 'runtime'))
    with (root / 'qs.log').open('w+') as log:
        server = subprocess.Popen(['qs', '-p', str(root), '--no-color'], env=env, stdout=log, stderr=log)

        def ipc(target, method, *args):
            return subprocess.run(['qs', 'ipc', '-p', str(root), 'call', '--', target, method, *args], env=env, text=True, capture_output=True, timeout=3)

        def check(method, expected, *args, probe='opened'):
            p = ipc(target, method, *args)
            assert p.returncode == 0, method + ': ' + p.stderr
            observed = ipc('verification', probe)
            if observed.stdout.strip() != expected:
                log.flush(); log.seek(0); print(log.read())
                raise AssertionError(method + ' -> ' + observed.stdout.strip() + ' (wanted ' + expected + ')')
        try:
            for attempt in range(30):
                p = ipc('verification', 'opened')
                if p.returncode == 0:
                    break
                if server.poll() is not None:
                    log.seek(0); raise RuntimeError(log.read())
                time.sleep(0.1)
            else:
                raise RuntimeError('IPC harness did not start')
            assert p.stdout.strip() == 'false', p.stdout
            check('open', 'true'); check('close', 'false'); check('toggle', 'true'); check('hide', 'false'); check('show', 'true')
            check('chooser', 'true', probe='chooseMode'); check('open', 'false', probe='chooseMode')
            check('page', 'lab', 'lab', probe='active'); check('page', 'lab', 'bogus', probe='active')
            check('enable', 'false', 'false', probe='enabled'); check('enable', 'true', 'true', probe='enabled')
            check('optimize', 'true', probe='optimized'); check('optimize', 'feel', probe='active')
            status = ipc(target, 'status')
            assert status.returncode == 0 and json.loads(status.stdout)['active'] == 'feel', status.stdout
            print('Trackpad Pulse IPC verified in an isolated offscreen Quickshell instance: open, close, show, hide, toggle, chooser, page, enable, optimize and status.')
        finally:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()
