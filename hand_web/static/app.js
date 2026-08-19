const state = {
  devices: [],
  defaults: {},
  device: null,
  transport: null,
  connected: false,
  activeSide: 'right',
  enabledSides: ['right'],
  values: {},
  actual: {},
  liveTimer: null,
  pendingCommands: new Set(),
  stopping: false,
  initializedFromState: new Set(),
};

const $ = (id) => document.getElementById(id);
const handPreview = new window.HandModelPreview($('handStage'), $('modelState'));

function log(message, tone = '') {
  const row = document.createElement('div');
  row.className = `activity-row ${tone}`;
  const time = document.createElement('time');
  time.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const text = document.createElement('span');
  text.textContent = message;
  row.append(time, text);
  $('activityList').prepend(row);
  while ($('activityList').children.length > 12) $('activityList').lastElementChild.remove();
}

function toast(message, tone = '') {
  const element = $('toast');
  element.textContent = message;
  element.className = `toast visible ${tone}`;
  clearTimeout(element.hideTimer);
  element.hideTimer = setTimeout(() => { element.className = 'toast'; }, 2600);
}

async function request(path, body) {
  const options = { cache: 'no-store' };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || '请求失败');
  return payload;
}

function currentTransport() {
  return state.device?.transports.find((item) => item.id === state.transport) || null;
}

function defaultPositions() {
  return state.device.joints.map(() => 0);
}

function ensureSide(side) {
  if (!state.values[side]) state.values[side] = defaultPositions();
  if (!state.actual[side]) state.actual[side] = null;
}

function targetReady(side = state.activeSide) {
  return state.initializedFromState.has(side);
}

function cancelLiveTimer() {
  clearTimeout(state.liveTimer);
  state.liveTimer = null;
}

function updateControlAvailability() {
  const ready = state.connected && targetReady() && !state.stopping;
  $('sendButton').disabled = !ready;
  $('stopButton').disabled = !state.connected || state.stopping;
  $('liveToggle').disabled = !ready;
  document.querySelectorAll('[data-joint], [data-joint-value]').forEach((input) => {
    input.disabled = !ready;
  });
  document.querySelectorAll('[data-action]').forEach((button) => {
    button.disabled = state.connected && !ready;
  });
}

function selectDevice(deviceId) {
  state.device = state.devices.find((item) => item.id === deviceId) || state.devices[0];
  const configured = state.defaults[state.device.id] || {};
  state.transport = configured.default_transport || state.device.transports[0]?.id;
  $('transportSelect').innerHTML = state.device.transports
    .map((item) => `<option value="${item.id}">${item.name}</option>`).join('');
  $('transportSelect').value = state.transport;
  $('deviceSubtitle').textContent = `${state.device.manufacturer} ${state.device.model}`;
  state.values = {};
  state.actual = {};
  state.initializedFromState.clear();
  renderTransport();
  renderQuickActions();
  renderJoints();
}

function renderTransport() {
  state.transport = $('transportSelect').value;
  const transport = currentTransport();
  const configured = state.defaults[state.device.id]?.[state.transport] || {};
  $('transportDescription').textContent = transport?.description || '';
  $('connectionFields').innerHTML = '';
  (transport?.connection_fields || []).forEach((spec) => {
    const label = document.createElement('label');
    label.className = 'field';
    const caption = document.createElement('span');
    caption.textContent = spec.label;
    let input;
    if (spec.type === 'select') {
      input = document.createElement('select');
      (spec.options || []).forEach((option) => {
        const element = document.createElement('option');
        element.value = option.value;
        element.textContent = option.label;
        input.append(element);
      });
    } else {
      input = document.createElement('input');
      input.type = spec.type || 'text';
      input.placeholder = spec.placeholder || '';
      if (spec.minimum !== undefined) input.min = spec.minimum;
      if (spec.maximum !== undefined) input.max = spec.maximum;
    }
    input.dataset.connectionField = spec.id;
    input.value = configured[spec.id] ?? spec.value ?? '';
    label.append(caption, input);
    $('connectionFields').append(label);
  });
  $('durationField').classList.toggle('disabled', !transport?.supports_duration);
  $('durationInput').disabled = !transport?.supports_duration;
}

function connectionOptions() {
  const result = {};
  document.querySelectorAll('[data-connection-field]').forEach((input) => {
    const value = input.type === 'number' ? Number(input.value) : input.value.trim();
    result[input.dataset.connectionField] = value;
  });
  return result;
}

function deriveSides(options) {
  if (state.transport === 'modbus') return [options.side || 'right'];
  if (options.sides === 'both') return ['left', 'right'];
  return [options.sides || 'right'];
}

function renderSideTabs() {
  $('sideTabs').innerHTML = state.enabledSides.map((side) => (
    `<button class="side-tab ${side === state.activeSide ? 'active' : ''}" data-side="${side}" role="tab">${side === 'left' ? '左手' : '右手'}</button>`
  )).join('');
  document.querySelectorAll('.side-tab').forEach((button) => {
    button.addEventListener('click', () => {
      state.activeSide = button.dataset.side;
      ensureSide(state.activeSide);
      renderSideTabs();
      renderJoints();
      updateHandPreview();
      updateControlAvailability();
    });
  });
  $('previewSide').textContent = state.activeSide === 'left' ? '左手' : '右手';
}

function renderJoints() {
  if (!state.device) return;
  ensureSide(state.activeSide);
  const values = state.values[state.activeSide];
  const actual = state.actual[state.activeSide];
  $('jointList').innerHTML = state.device.joints.map((joint, index) => {
    const value = Math.round(values[index] * 100);
    const actualValue = actual ? Math.round(actual[index] * 100) : null;
    return `<div class="joint-row">
      <div class="joint-label"><strong>${joint.name}</strong><span>${joint.english_name}</span></div>
      <div class="slider-stack">
        <input type="range" min="0" max="100" step="1" value="${value}" data-joint="${index}" ${state.connected && targetReady() && !state.stopping ? '' : 'disabled'}>
        <div class="actual-track"><i style="left:${actualValue ?? 0}%" class="${actualValue === null ? 'hidden' : ''}"></i></div>
      </div>
      <label class="value-box"><input type="number" min="0" max="100" value="${value}" data-joint-value="${index}" ${state.connected && targetReady() && !state.stopping ? '' : 'disabled'}><span>%</span></label>
    </div>`;
  }).join('');

  document.querySelectorAll('[data-joint]').forEach((input) => {
    input.addEventListener('input', (event) => updateJoint(Number(event.target.dataset.joint), Number(event.target.value)));
  });
  document.querySelectorAll('[data-joint-value]').forEach((input) => {
    input.addEventListener('change', (event) => updateJoint(Number(event.target.dataset.jointValue), Number(event.target.value)));
  });
  updateHandPreview();
  updateControlAvailability();
}

function updateActualMarkers() {
  const actual = state.actual[state.activeSide];
  if (!actual) return;
  document.querySelectorAll('.actual-track i').forEach((marker, index) => {
    marker.style.left = `${Math.round(actual[index] * 100)}%`;
    marker.classList.remove('hidden');
  });
}

function updateJoint(index, percent) {
  const value = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0));
  state.values[state.activeSide][index] = value / 100;
  const slider = document.querySelector(`[data-joint="${index}"]`);
  const number = document.querySelector(`[data-joint-value="${index}"]`);
  slider.value = value;
  number.value = value;
  updateHandPreview();
  if ($('liveToggle').checked) scheduleLiveCommand();
}

function updateHandPreview() {
  if (!state.device) return;
  ensureSide(state.activeSide);
  handPreview.setPose(state.activeSide, state.values[state.activeSide]);
}

function renderQuickActions() {
  $('quickActions').innerHTML = (state.device.quick_actions || []).map((action) => (
    `<button class="quick-button" data-action="${action.id}">${action.name}</button>`
  )).join('');
  document.querySelectorAll('[data-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = state.device.quick_actions.find((item) => item.id === button.dataset.action);
      state.values[state.activeSide] = action.positions.slice();
      renderJoints();
      if (state.connected) sendCommand();
    });
  });
  updateControlAvailability();
}

function setConnectionState(connected, status = {}) {
  state.connected = connected;
  $('connectButton').disabled = connected;
  $('disconnectButton').disabled = !connected;
  $('deviceSelect').disabled = connected;
  $('transportSelect').disabled = connected;
  document.querySelectorAll('[data-connection-field]').forEach((input) => { input.disabled = connected; });
  $('statusDot').className = `status-dot ${connected ? 'online' : ''}`;
  $('statusLabel').textContent = connected ? '控制链路已连接' : '未连接';
  $('statusDetail').textContent = connected ? `${state.device.name} / ${currentTransport().name}` : '请选择通信方式';
  if (!connected) {
    cancelLiveTimer();
    $('liveToggle').checked = false;
    state.stopping = false;
    state.initializedFromState.clear();
    state.actual = {};
  }
  renderJoints();
  renderQuickActions();
  updateControlAvailability();
  updateFacts(status);
}

function updateFacts(status) {
  const facts = $('connectionFacts').querySelectorAll('strong');
  facts[0].textContent = state.device?.name || '--';
  facts[1].textContent = state.connected ? currentTransport()?.name : '--';
  const hands = Object.values(status.hands || {});
  const online = hands.filter((hand) => hand.online).length;
  facts[2].textContent = state.connected ? `${online}/${hands.length || state.enabledSides.length} 在线` : '--';
}

async function connectDevice() {
  const options = connectionOptions();
  $('connectButton').disabled = true;
  $('connectButton').textContent = '正在连接';
  try {
    cancelLiveTimer();
    state.initializedFromState.clear();
    state.actual = {};
    const result = await request('/api/connect', {
      device_id: state.device.id,
      transport: state.transport,
      options,
    });
    state.enabledSides = deriveSides(options);
    state.activeSide = state.enabledSides.includes(state.activeSide) ? state.activeSide : state.enabledSides[0];
    state.enabledSides.forEach(ensureSide);
    renderSideTabs();
    setConnectionState(true, result);
    log(result.message || '设备已连接', 'success');
    toast(result.message || '设备已连接');
    await pollStatus();
    if (!targetReady()) log('正在同步设备关节状态，暂不能执行姿态', 'warning');
  } catch (error) {
    setConnectionState(false);
    log(`连接失败：${error.message}`, 'error');
    toast(error.message, 'error');
  } finally {
    $('connectButton').textContent = '连接设备';
    if (!state.connected) $('connectButton').disabled = false;
  }
}

async function disconnectDevice() {
  try {
    const result = await request('/api/disconnect', {});
    setConnectionState(false);
    log(result.message || '设备已断开');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function sendCommand(silent = false) {
  if (!state.connected || state.stopping) return;
  if (!targetReady()) {
    if (!silent) toast('正在等待设备关节状态，请稍后再试', 'error');
    return;
  }
  const target = state.values[state.activeSide];
  const actual = state.actual[state.activeSide];
  if (Array.isArray(actual) && target.every((value, index) => Math.abs(value - actual[index]) <= 0.0005)) {
    if (!silent) {
      log('当前已是该姿态，未发送指令');
      toast('当前已是该姿态');
    }
    return;
  }
  const operation = request('/api/command', {
    side: state.activeSide,
    positions: target,
    duration_ms: Number($('durationInput').value),
    continuous: silent,
  });
  state.pendingCommands.add(operation);
  try {
    const result = await operation;
    if (!silent) {
      log(result.message || '姿态指令已下发', 'success');
      toast(result.message || '姿态指令已下发');
    }
  } catch (error) {
    log(`发送失败：${error.message}`, 'error');
    toast(error.message, 'error');
  } finally {
    state.pendingCommands.delete(operation);
  }
}

function scheduleLiveCommand() {
  clearTimeout(state.liveTimer);
  state.liveTimer = setTimeout(() => sendCommand(true), 90);
}

async function stopMotion() {
  cancelLiveTimer();
  $('liveToggle').checked = false;
  state.stopping = true;
  updateControlAvailability();
  try {
    await Promise.allSettled([...state.pendingCommands]);
    const result = await request('/api/stop', {});
    log(result.message || '已停止', 'warning');
    toast(result.message || '已停止');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    state.stopping = false;
    updateControlAvailability();
  }
}

async function pollStatus() {
  try {
    const status = await request('/api/status');
    if (!status.connected) {
      if (state.connected) setConnectionState(false, status);
      return;
    }
    if (!state.connected) {
      state.enabledSides = Object.keys(status.hands || {});
      if (!state.enabledSides.length) state.enabledSides = ['right'];
      if (!state.enabledSides.includes(state.activeSide)) state.activeSide = state.enabledSides[0];
      renderSideTabs();
      setConnectionState(true, status);
    }
    let shouldRender = false;
    Object.entries(status.hands || {}).forEach(([side, hand]) => {
      ensureSide(side);
      if (Array.isArray(hand.positions)) {
        state.actual[side] = hand.positions.slice();
        if (!state.initializedFromState.has(side)) {
          state.values[side] = hand.positions.slice();
          state.initializedFromState.add(side);
          if (side === state.activeSide) shouldRender = true;
        }
      }
    });
    $('statusDot').className = `status-dot ${Object.values(status.hands || {}).some((hand) => hand.online) ? 'online' : 'waiting'}`;
    $('statusLabel').textContent = Object.values(status.hands || {}).some((hand) => hand.online) ? '设备在线' : '链路已连接';
    $('statusDetail').textContent = status.error || `${state.device.name} / ${currentTransport().name}`;
    updateFacts(status);
    if (shouldRender) renderJoints();
    else updateActualMarkers();
  } catch (error) {
    $('statusDot').className = 'status-dot error';
    $('statusLabel').textContent = '服务异常';
    $('statusDetail').textContent = error.message;
  }
}

async function initialize() {
  try {
    const payload = await request('/api/devices');
    state.devices = payload.devices;
    state.defaults = payload.defaults || {};
    $('deviceSelect').innerHTML = state.devices.map((device) => `<option value="${device.id}">${device.name}</option>`).join('');
    $('deviceSelect').value = payload.default_device;
    selectDevice(payload.default_device);
    state.enabledSides = ['right'];
    ensureSide('right');
    renderSideTabs();
    setConnectionState(false);
    log('调试服务已就绪');
    await pollStatus();
  } catch (error) {
    $('statusLabel').textContent = '服务未启动';
    $('statusDetail').textContent = error.message;
  }
}

$('deviceSelect').addEventListener('change', (event) => selectDevice(event.target.value));
$('transportSelect').addEventListener('change', renderTransport);
$('connectButton').addEventListener('click', connectDevice);
$('disconnectButton').addEventListener('click', disconnectDevice);
$('sendButton').addEventListener('click', () => sendCommand());
$('stopButton').addEventListener('click', stopMotion);
$('durationInput').addEventListener('input', () => { $('durationOutput').textContent = `${$('durationInput').value} ms`; });
$('clearLogButton').addEventListener('click', () => { $('activityList').innerHTML = ''; });
document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault();
    sendCommand();
  }
  if (event.key === 'Escape' && state.connected) stopMotion();
});

initialize();
setInterval(pollStatus, 800);
