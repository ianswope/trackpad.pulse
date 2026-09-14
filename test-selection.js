const vm = require('node:vm');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const qml = fs.readFileSync(path.join(__dirname, 'Panel.qml'), 'utf8');

// Execute the real QML functions; callback ordering is controlled by each test.
function context() {
  const settings = { enabled: true, natural_scroll: false, tap_to_click: true,
    disable_while_typing: true, clickfinger_behavior: true, accel_profile: 'adaptive',
    scroll_factor: 0.2, sensitivity: 0.1 };
  const ctx = {
    devices: [
      { id: 'apple', label: 'Apple', connected: true, names: ['apple'], settings: { ...settings } },
      { id: 'dell', label: 'Dell', connected: true, names: ['dell'], settings: { ...settings, sensitivity: 0.3 } }
    ],
    selectedDevice: 'apple', pendingActions: [], settingsError: '',
    editGeneration: 0, stateGeneration: 0, refreshPending: false,
    actionProc: { running: false }, stateProc: { running: false }, backend: 'trackpads.py',
    Model: require('./Model.js'),
    Curve: require('./Curve.js'), previousFeels: {}, curveEditor: {},
    scrollDebounce: { running: false, stop() { this.running = false; } },
    pointerDebounce: { running: false, stop() { this.running = false; } }
  };
  vm.createContext(ctx);
  const functions = qml.match(/^  function \w+\([^\n]*\) \{[^\n]*\}$|^  function \w+\([^\n]*\) \{\n[\s\S]*?^  \}/gm);
  for (const source of functions) vm.runInContext(source, ctx);
  ctx.loadSelection();
  return ctx;
}

{
  const ctx = context();
  ctx.selectDevice('dell');
  ctx.actionProc.running = true;
  ctx.scrollDebounce.running = ctx.pointerDebounce.running = true;
  ctx.pendingScrollFactor = 0.6;
  ctx.pendingPointerSpeed = 0.8;
  ctx.selectDevice('apple');
  assert.equal(ctx.pendingActions.length, 2);
  assert.ok(ctx.pendingActions.every(x => x.device === 'dell'));
  assert.equal(ctx.pendingActions[0].value, 0.6);
  assert.equal(ctx.pendingActions[1].value, 0.8);
  assert.equal(ctx.pointerSpeed, 0.1);
  ctx.togglePointerAcceleration();
  assert.equal(ctx.pendingActions[2].device, 'apple');
  assert.equal(ctx.pendingActions[2].value, 'flat');
  ctx.selectDevice('dell');
  assert.equal(ctx.pointerAcceleration, true);
}

{
  const ctx = context();
  const oldRead = JSON.stringify({ devices: ctx.devices });
  ctx.refresh();
  ctx.toggleNaturalScroll();
  assert.equal(ctx.naturalScroll, true);
  ctx.actionProc.running = false;
  ctx.finishAction(0); // The old state process is still running.
  ctx.receiveState(oldRead);
  assert.equal(ctx.naturalScroll, true, 'late read must not revert the successful edit');
  ctx.stateProc.running = false;
  ctx.finishStateRead(0);
  assert.equal(ctx.stateProc.running, true, 'discarding a stale read must schedule a fresh read');
  ctx.receiveState(JSON.stringify({ devices: ctx.devices }));
  ctx.stateProc.running = false;
  ctx.finishStateRead(0);
  assert.equal(ctx.stateProc.running, false, 'a successful read must not poll in a tight loop');
  ctx.toggleNaturalScroll();
  assert.equal(JSON.parse(ctx.actionProc.command.at(-1)), false, 'next click must reverse the edit');
}

{
  const ctx = context();
  ctx.refresh();
  ctx.scrollDebounce.running = true;
  ctx.receiveState(JSON.stringify({ devices: ctx.devices }));
  ctx.stateProc.running = false;
  ctx.finishStateRead(0);
  assert.equal(ctx.stateProc.running, false, 'refresh must wait for pending slider edits');
  ctx.scrollDebounce.running = false;
  ctx.pendingScrollFactor = 0.7;
  ctx.commitScrollFactor();
  ctx.actionProc.running = false;
  ctx.finishAction(0);
  assert.equal(ctx.stateProc.running, true);
}

{
  const ctx = context();
  ctx.toggleNaturalScroll();
  ctx.togglePointerAcceleration();
  ctx.actionProc.running = false;
  ctx.finishAction(124);
  assert.match(ctx.settingsError, /Could not save/);
  assert.equal(ctx.actionProc.running, true, 'a timed-out write must release the next queued write');
  assert.equal(ctx.actionProc.command.at(-2), 'accel_profile');
  ctx.actionProc.running = false;
  ctx.finishAction(0);
  assert.equal(ctx.stateProc.running, true);
  ctx.stateProc.running = false;
  ctx.settingsError = '';
  ctx.finishStateRead(124);
  assert.match(ctx.settingsError, /Could not read/);
  assert.equal(ctx.stateProc.running, false, 'a failed read must not immediately retry forever');
  ctx.refresh();
  assert.equal(ctx.stateProc.running, true, 'a subsequent poll must recover after a read timeout');
}

{
  const ctx = context();
  const argv = ctx.bounded(0.05, ['python3', '-c', 'import time; time.sleep(60)']);
  const result = spawnSync(argv[0], Array.from(argv.slice(1)), { timeout: 4000 });
  assert.ifError(result.error);
  assert.equal(result.status, 124, 'the actual timeout wrapper must reap a stalled helper');
  assert.match(qml, /command: root\.bounded\(15, \["python3", root\.backend, "state"\]\)/);
  ctx.toggleNaturalScroll();
  assert.deepEqual(Array.from(ctx.actionProc.command.slice(0, 4)), ['timeout', '-k', '2', '10']);
}

{
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'manifest.json'), 'utf8'));
  assert.equal(manifest.id, 'nixfred.trackpad-pulse');
  assert.match(qml, /ipcTarget: "nixfred\.trackpad-pulse"/);
  assert.match(qml, /manageIpc: false/);
  for (const verb of ['open', 'close', 'show', 'hide', 'toggle']) assert.match(qml, new RegExp('function ' + verb + '\\(\\): void'), 'own IpcHandler keeps the ' + verb + ' verb');
  assert.match(qml, /root\.receiveState\(String\(text\)\)/);
  assert.match(qml, /Qt\.callLater\(function\(\) \{ root\.finishStateRead\(code\) \}\)/);
  assert.match(qml, /Qt\.callLater\(function\(\) \{ root\.finishAction\(code\) \}\)/);
}
{
  const ctx = context();
  ctx.scrollDebounce.restart = function() { this.running = true; };
  ctx.setScrollFactor(0.05);
  ctx.focusSection = 'scroll';
  ctx.moveCursorH(-1);
  assert.equal(ctx.scrollFactor, 0.04);
  ctx.selectDevice('dell'); // Flush the precise value to the original device.
  assert.equal(JSON.parse(ctx.actionProc.command.at(-1)), 0.04);
  assert.equal(ctx.actionProc.command.at(-3), 'apple');
  assert.equal(ctx.scrollFactor, 0.2);
  ctx.setScrollFactor(0);
  assert.equal(ctx.scrollFactor, 0.01);
  ctx.setScrollFactor(0.056);
  assert.equal(ctx.scrollFactor, 0.06);
}
console.log('Passed: device selection, fine scroll steps, stale-read rejection, debounce ordering, timeout recovery, IPC configuration, and Pulse readouts.');

{
  const ctx = context();
  ctx.actionProc.running = true;
  const original = JSON.stringify(ctx.pointerFeel);
  ctx.applyPointerFeel({profile: 'mac', curve: ctx.Curve.defaults()});
  assert.equal(ctx.devices[0].settings.accel_profile, 'custom');
  assert.equal(ctx.pointerFeel.profile, 'mac');
  ctx.selectDevice('dell');
  assert.equal(ctx.pointerFeel.profile, 'adaptive');
  assert.equal(ctx.previousFeels.dell, undefined);
  assert.equal(ctx.pendingActions[0].device, 'apple');
  ctx.selectDevice('apple');
  ctx.restorePointerFeel();
  assert.equal(JSON.stringify(ctx.pointerFeel), original);
  assert.equal(ctx.pendingActions[1].value.profile, 'adaptive');
  assert.equal(ctx.devices[1].settings.accel_profile, 'adaptive');
}

{
  const ctx = context();
  ctx.previousFeels.apple = {profile: 'flat', curve: ctx.Curve.defaults()};
  ctx.loadSelection();
  assert.equal(ctx.previousFeels.apple, undefined, 'authoritative state clears stale undo');
  ctx.devices = [];
  ctx.loadSelection();
  assert.equal(ctx.deviceName, '', 'removed devices must not remain actionable');
  assert.equal(ctx.deviceConnected, false);
  const backendExpression = qml.match(/readonly property string backend: (.*)/)[1];
  ctx.Qt = {resolvedUrl: () => 'file:///tmp/plugin%20with%20spaces/trackpads.py'};
  assert.equal(vm.runInContext(backendExpression, ctx), '/tmp/plugin with spaces/trackpads.py');
}

{
  const Curve = require('./Curve.js');
  const curve = Curve.defaults();
  const samples = Curve.points(curve);
  for (let index = 0; index <= 160; index++) {
    assert.equal(Curve.sampledGain(curve, index / 40, samples), Curve.sampledGain(curve, index / 40));
  }
}

{
  const ctx = context();
  ctx.actionProc.running = true;
  for (let i = 0; i < 1000; i++) ctx.enqueue('scroll_factor', 0.01 + (i % 100) / 100);
  assert.equal(ctx.pendingActions.length, 1, 'repeated scalar updates should coalesce');
  assert.equal(ctx.pendingActions[0].value, 1);
  for (let i = 0; i < 200; i++) ctx.enqueue(i % 2 ? 'natural_scroll' : 'tap_to_click', true);
  assert.equal(ctx.pendingActions.length, 128, 'pending actions must have a fixed memory bound');
  assert.match(ctx.settingsError, /Too many/);
}

{
  const ctx = context();
  ctx.actionProc.running = true;
  ctx.scrollDebounce.running = true;
  ctx.pendingScrollFactor = 0.4;
  ctx.setScrollScale(3);
  assert.equal(ctx.pendingActions[0].option, 'scroll_factor');
  assert.equal(ctx.pendingActions[0].value, 0.4, 'pending edit must use its original scale');
  assert.equal(ctx.pendingActions[1].option, 'scroll_scale');
  assert.equal(ctx.pendingActions[1].value, 3);
  assert.equal(ctx.devices[0].settings.scroll_factor, 1.2);
  ctx.loadSelection();
  assert.ok(Math.abs(ctx.scrollFactor - 0.4) < 1e-9);
  ctx.pendingScrollFactor = 1;
  ctx.commitScrollFactor();
  assert.equal(ctx.pendingActions[2].value, 3, 'full slider reaches the configured scale');
  ctx.selectDevice('dell');
  assert.equal(ctx.scrollScale, 1);
  assert.equal(ctx.scrollFactor, 0.2);
  assert.equal(ctx.Model.clampScrollFactor(3), 1);
}

{
  const Pulse = require('./Pulse.js');
  assert.equal(Pulse.distance(999), '999 mm');
  assert.equal(Pulse.distance(12345), '12.3 m');
  assert.equal(Pulse.speed(84.4), '84 mm/s');
  assert.equal(Pulse.duration(4000), '1 h 07 min');
  const hist = new Array(31).fill(0); hist[3] = 2; hist[10] = 1; hist[20] = 1;
  assert.equal(Pulse.percentile(hist, 5, 0.5), 20, 'median lands at the top of the bin that crosses half');
  assert.equal(Pulse.percentile(hist, 5, 0.9), 105);
  assert.ok(Math.abs(Pulse.shareBelow(hist, 5, 52.5) - 0.625) < 1e-9, 'a partial bin is pro-rated');
  assert.ok(Math.abs(Pulse.curveToMm(4, 25.4) - 101.6) < 1e-9);
  const snap = { warm: true, ts: 100, access: 'evdev', lastTouch: 90, pads: [{ hz: 125 }], today: { counts: { touches: 42, taps: 3, taps2: 1, taps3: 0, clicks: 2, palms: 1, distance: 1234, active: 61 }, peak: 220 } };
  assert.equal(Pulse.readout(snap, {}, 0), '42');
  assert.equal(Pulse.readout(snap, {}, 2), '4');
  assert.equal(Pulse.readout(snap, {}, 5), '125 Hz');
  assert.equal(Pulse.verdict(snap, { pads: [{ fingers: [{ x: 0.5, y: 0.5, palm: false }], speed: 200 }] }, true, true, 100), 'FAST SWIPE');
  assert.equal(Pulse.verdict(snap, {}, false, true, 100), 'TRACKPAD OFF');
  assert.equal(Pulse.verdict({ warm: true, ts: 0, access: 'evdev' }, {}, true, true, 100), 'RECORDER STALE');
  assert.equal(Pulse.verdict(Object.assign({}, snap, { access: 'cursor' }), {}, true, true, 100), 'CURSOR ONLY · NO PAD ACCESS');
  assert.equal(Pulse.readout(Object.assign({}, snap, { access: 'cursor', today: { counts: {}, cursor: { distance: 4321 } } }), {}, 1), '4,321 px');
  const two = { pads: [{ device: 'apple', presetScale: 0.78 }, { device: 'elan', presetScale: 1.2 }], weekByDevice: { apple: [1, 2], elan: [0, 0] } };
  assert.equal(Pulse.presetScale(two, 'elan'), 1.2);
  assert.equal(Pulse.presetScale(two, 'dell'), 1, 'an unmeasured pad keeps the preset as shipped');
  assert.deepEqual(Pulse.padHist(two, 'apple', [9]), [1, 2]);
  assert.deepEqual(Pulse.padHist(two, 'elan', [9]), [9], 'no history of its own yet: the combined one');
  assert.deepEqual(Pulse.padHist({ pads: [{ device: 'apple' }], weekByDevice: { apple: [1] } }, 'apple', [9]), [9], 'one pad: unchanged');
  const CurveJs = require('./Curve.js');
  assert.deepEqual(CurveJs.presetFor(1, 1), CurveJs.presetForScale(1));
  assert.deepEqual(CurveJs.presetFor(1, 0.5), { precision: 0.09375, start: 0.8, end: 2.8, fast: 0.5 });
  assert.deepEqual(CurveJs.presetFor(1, 1.5), { precision: 0.28125, start: 0.8, end: 2.8, fast: 1 }, 'Fast stops at the ceiling');
}
