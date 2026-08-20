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
  poses: [],
  editingPoseId: null,
  calibration: {},
  vision: { available: null, starting: false, running: false, stopping: false, dry_run: false },
  visionFrameLoading: false,
  browserCameraStream: null,
  browserFrameTimer: null,
  browserFrameSending: false,
};

const $ = (id) => document.getElementById(id);
const handPreview = new window.HandModelPreview($('handStage'), $('modelState'));

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;',
  }[character]));
}

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
  const manualLocked = state.vision.starting || state.vision.running || state.vision.stopping;
  const ready = state.connected && targetReady() && !state.stopping && !manualLocked;
  $('sendButton').disabled = !ready;
  $('stopButton').disabled = !state.connected || state.stopping || manualLocked;
  $('liveToggle').disabled = !ready;
  $('connectButton').disabled = state.connected || manualLocked;
  $('disconnectButton').disabled = !state.connected || manualLocked;
  $('deviceSelect').disabled = state.connected || manualLocked;
  $('transportSelect').disabled = state.connected || manualLocked;
  document.querySelectorAll('[data-connection-field]').forEach((input) => {
    input.disabled = state.connected || manualLocked;
  });
  document.querySelectorAll('[data-joint], [data-joint-value]').forEach((input) => {
    input.disabled = !ready;
  });
  document.querySelectorAll('[data-pose-action], #addPoseButton').forEach((button) => {
    button.disabled = manualLocked || (state.connected && !ready);
  });
  const dryRun = $('visionDryRun').checked;
  $('startVisionButton').textContent = '启动视觉控制';
  $('startVisionButton').disabled = state.vision.starting || state.vision.running || state.vision.stopping
    || state.vision.available === false || (!state.connected && !dryRun);
  $('stopVisionButton').disabled = !state.vision.running || state.vision.stopping;
  $('cameraInput').disabled = manualLocked;
  $('visionSourceSelect').disabled = manualLocked;
  $('visionSideSelect').disabled = manualLocked;
  $('visionDryRun').disabled = manualLocked;
  $('calibrationButton').disabled = !state.vision.running || !state.vision.dry_run
    || Boolean(state.calibration.active_stage);
  document.querySelectorAll('[data-calibration-stage]').forEach((button) => {
    button.disabled = !state.vision.running || !state.vision.dry_run
      || Boolean(state.calibration.active_stage);
  });
}

function renderVisionSource() {
  const serverSource = $('visionSourceSelect').value === 'server';
  $('serverCameraField').hidden = !serverSource;
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
  state.poses = [];
  state.initializedFromState.clear();
  renderTransport();
  renderPoseLibrary();
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
  if (!state.vision.running) $('visionSideSelect').value = state.activeSide;
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
      <label class="joint-value target-value"><span>设置</span><span class="value-box"><input type="number" min="0" max="100" value="${value}" data-joint-value="${index}" ${state.connected && targetReady() && !state.stopping ? '' : 'disabled'}><i>%</i></span></label>
      <div class="joint-value actual-value"><span>当前</span><output class="value-box" data-joint-actual="${index}">${actualValue === null ? '--' : `${actualValue}<i>%</i>`}</output></div>
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
  if (!actual) {
    document.querySelectorAll('[data-joint-actual]').forEach((output) => { output.textContent = '--'; });
    return;
  }
  document.querySelectorAll('.actual-track i').forEach((marker, index) => {
    const percent = Math.round(actual[index] * 100);
    marker.style.left = `${percent}%`;
    marker.classList.remove('hidden');
    const output = document.querySelector(`[data-joint-actual="${index}"]`);
    if (output) output.innerHTML = `${percent}<i>%</i>`;
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
  handPreview.setPose(state.activeSide, state.values[state.activeSide], state.device.preview);
}

function renderPoseLibrary() {
  if (!state.device) return;
  if (!state.poses.length) {
    $('poseList').innerHTML = '<div class="pose-empty">暂无保存的姿态</div>';
  } else {
    $('poseList').innerHTML = state.poses.map((pose) => `
      <div class="pose-item">
        <div class="pose-copy">
          <strong>${escapeHtml(pose.description_zh)}</strong>
          <span>${escapeHtml(pose.name_en)}</span>
        </div>
        <div class="pose-actions">
          <button class="pose-command" type="button" data-pose-action="apply" data-pose-id="${escapeHtml(pose.id)}">载入</button>
          <button class="icon-button pose-icon" type="button" data-pose-action="edit" data-pose-id="${escapeHtml(pose.id)}" title="编辑姿态" aria-label="编辑姿态">&#9998;</button>
          <button class="icon-button pose-icon danger-icon" type="button" data-pose-action="delete" data-pose-id="${escapeHtml(pose.id)}" title="删除姿态" aria-label="删除姿态">&times;</button>
        </div>
      </div>`).join('');
  }
  document.querySelectorAll('[data-pose-action]').forEach((button) => {
    button.addEventListener('click', () => handlePoseAction(button.dataset.poseAction, button.dataset.poseId));
  });
  updateControlAvailability();
}

async function loadPoses() {
  if (!state.device) return;
  try {
    const payload = await request(`/api/poses?device_id=${encodeURIComponent(state.device.id)}`);
    state.poses = payload.poses || [];
  } catch (error) {
    state.poses = [];
    log(`姿态库加载失败：${error.message}`, 'error');
    toast(error.message, 'error');
  }
  renderPoseLibrary();
}

function openPoseDialog(pose = null) {
  ensureSide(state.activeSide);
  state.editingPoseId = pose?.id || null;
  $('poseDialogTitle').textContent = pose ? '编辑姿态' : '新增姿态';
  $('poseNameEn').value = pose?.name_en || '';
  $('poseDescriptionZh').value = pose?.description_zh || '';
  const positions = pose?.positions || state.values[state.activeSide];
  $('poseJointFields').innerHTML = state.device.joints.map((joint, index) => `
    <label>
      <span><strong>${escapeHtml(joint.name)}</strong><small>${escapeHtml(joint.english_name)}</small></span>
      <span class="pose-joint-input"><input type="number" min="0" max="100" step="1" value="${Math.round(positions[index] * 100)}" data-pose-joint="${index}" required><i>%</i></span>
    </label>`).join('');
  $('poseDialog').showModal();
  $('poseNameEn').focus();
}

function closePoseDialog() {
  state.editingPoseId = null;
  $('poseDialog').close();
}

function handlePoseAction(action, poseId) {
  const pose = state.poses.find((item) => item.id === poseId);
  if (!pose) return;
  if (action === 'apply') {
    state.values[state.activeSide] = pose.positions.slice();
    renderJoints();
    log(`已载入姿态：${pose.description_zh}`);
    toast('姿态已载入，请确认后执行');
  } else if (action === 'edit') {
    openPoseDialog(pose);
  } else if (action === 'delete') {
    deletePose(pose);
  }
}

async function savePose(event) {
  event.preventDefault();
  const positions = [...document.querySelectorAll('[data-pose-joint]')]
    .map((input) => Math.max(0, Math.min(100, Number(input.value))) / 100);
  try {
    const payload = await request('/api/poses/save', {
      id: state.editingPoseId || undefined,
      device_id: state.device.id,
      name_en: $('poseNameEn').value,
      description_zh: $('poseDescriptionZh').value,
      positions,
    });
    state.poses = payload.poses;
    const operation = state.editingPoseId ? '已更新姿态' : '已保存姿态';
    closePoseDialog();
    renderPoseLibrary();
    log(`${operation}：${payload.pose.description_zh}`, 'success');
    toast(operation);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function deletePose(pose) {
  if (!window.confirm(`确定删除“${pose.description_zh} / ${pose.name_en}”吗？`)) return;
  try {
    const payload = await request('/api/poses/delete', { device_id: state.device.id, id: pose.id });
    state.poses = payload.poses;
    renderPoseLibrary();
    log(`已删除姿态：${pose.description_zh}`, 'warning');
    toast('姿态已删除');
  } catch (error) {
    toast(error.message, 'error');
  }
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
  renderPoseLibrary();
  updateControlAvailability();
  updateFacts(status);
}

function renderVisionStatus(status) {
  const wasRunning = state.vision.running;
  state.vision = status;
  const active = status.starting || status.running;
  document.body.classList.toggle('vision-active', active);
  $('visionState').textContent = status.starting ? '正在启动'
    : status.running ? (status.dry_run ? '仅识别' : '控制中') : '未启动';
  $('visionState').className = `vision-state ${active ? 'active' : ''} ${status.error ? 'error' : ''}`;
  $('visionGesture').textContent = status.gesture === 'Fist' ? '握拳'
    : status.gesture === 'Open Hand' ? '张开' : status.running ? '识别中' : '--';
  const positions = Array.isArray(status.positions) ? status.positions : null;
  $('visionThumbFlex').textContent = positions ? `${Math.round(positions[0] * 100)}%` : '--';
  $('visionThumbAux').textContent = positions ? `${Math.round(positions[1] * 100)}%` : '--';
  $('visionTrackingSpace').textContent = status.tracking_space === 'world' ? '世界坐标'
    : status.tracking_space === 'normalized' ? '图像坐标' : '--';
  state.calibration = status.calibration || {};
  $('visionCalibrationState').textContent = state.calibration.profile_available ? '已标定' : '默认';
  $('visionCommands').textContent = String(status.commands || 0);
  $('visionFrames').textContent = `${status.frames || 0} 帧`;
  $('cameraFrameBadge').textContent = `${status.frames || 0} 帧`;
  $('cameraModeBadge').textContent = status.dry_run ? '仅识别' : '视觉控制';
  $('stopVisionButton').textContent = '停止视觉控制';
  $('visionDetail').textContent = status.error || (status.starting ? '正在打开摄像头'
    : status.running ? (status.last_detection_at ? '已检测' : status.source === 'browser' ? '等待浏览器画面' : '等待手部') : '待机');
  renderCalibration();
  if (status.running && Array.isArray(status.positions) && status.side) {
    ensureSide(status.side);
    state.values[status.side] = status.positions.slice();
    if (state.activeSide === status.side) {
      renderJoints();
      updateHandPreview();
    }
  }
  if (wasRunning && !status.running && !status.dry_run) {
    state.initializedFromState.delete(status.side || state.activeSide);
  }
  if (wasRunning && !status.running && status.source === 'browser') stopBrowserCamera();
  if (!active) {
    $('visionFrame').classList.remove('visible');
    $('cameraPlaceholder').textContent = status.error || (status.available === false ? '视觉依赖未安装' : '摄像头未启动');
    $('cameraPlaceholder').classList.remove('hidden');
  }
  updateControlAvailability();
}

function stopBrowserCamera() {
  clearTimeout(state.browserFrameTimer);
  state.browserFrameTimer = null;
  state.browserFrameSending = false;
  if (state.browserCameraStream) {
    state.browserCameraStream.getTracks().forEach((track) => track.stop());
    state.browserCameraStream = null;
  }
  $('browserCamera').srcObject = null;
}

async function openBrowserCamera() {
  stopBrowserCamera();
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error('浏览器摄像头需要 HTTPS，或通过 SSH 转发后访问 http://127.0.0.1:18089');
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: {
      facingMode: 'user',
      width: { ideal: 1280 },
      height: { ideal: 720 },
    },
  });
  const video = $('browserCamera');
  state.browserCameraStream = stream;
  video.srcObject = stream;
  await video.play();
}

function browserFrameBlob() {
  const video = $('browserCamera');
  if (!video.videoWidth || !video.videoHeight) return Promise.resolve(null);
  const scale = Math.min(1, 960 / video.videoWidth, 540 / video.videoHeight);
  const canvas = $('browserCapture');
  canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
  canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
  canvas.getContext('2d', { alpha: false }).drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.78));
}

async function sendBrowserFrame() {
  if (!state.browserCameraStream || !state.vision.running || state.vision.source !== 'browser') return;
  if (state.browserFrameSending) return;
  state.browserFrameSending = true;
  try {
    const frame = await browserFrameBlob();
    if (frame) {
      const response = await fetch('/api/vision/frame', {
        method: 'POST',
        headers: { 'Content-Type': 'image/jpeg' },
        body: frame,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || '浏览器摄像头帧上传失败');
      }
    }
  } catch (error) {
    $('visionDetail').textContent = error.message;
  } finally {
    state.browserFrameSending = false;
    if (state.browserCameraStream && state.vision.running && state.vision.source === 'browser') {
      state.browserFrameTimer = setTimeout(sendBrowserFrame, 80);
    }
  }
}

async function pollVisionStatus() {
  try {
    renderVisionStatus(await request('/api/vision/status'));
  } catch (error) {
    $('visionDetail').textContent = error.message;
  }
}

function refreshVisionFrame() {
  if (!state.vision.running || state.visionFrameLoading || !state.vision.last_frame_at) return;
  state.visionFrameLoading = true;
  $('visionFrame').src = `/api/vision/frame?t=${Date.now()}`;
}

async function startVision() {
  cancelLiveTimer();
  $('liveToggle').checked = false;
  $('startVisionButton').disabled = true;
  try {
    const dryRun = $('visionDryRun').checked;
    const source = $('visionSourceSelect').value;
    if (source === 'browser') await openBrowserCamera();
    const status = await request('/api/vision/start', {
      device_id: state.device.id,
      source,
      camera: Number($('cameraInput').value),
      side: $('visionSideSelect').value,
      dry_run: dryRun,
      control_enabled: !dryRun,
    });
    renderVisionStatus(status);
    if (source === 'browser') sendBrowserFrame();
    log(status.dry_run ? '视觉识别已启动' : '视觉控制已启动', 'success');
  } catch (error) {
    if (error.message.includes('视觉控制已经在运行')) {
      const status = await request('/api/vision/status');
      renderVisionStatus(status);
      if (status.starting || status.running) {
        log('已接入当前视觉会话', 'success');
        return;
      }
    }
    stopBrowserCamera();
    log(`视觉启动失败：${error.message}`, 'error');
    toast(error.message, 'error');
    await pollVisionStatus();
  }
}

async function stopVision() {
  state.vision.stopping = true;
  updateControlAvailability();
  try {
    const status = await request('/api/vision/stop', {});
    stopBrowserCamera();
    renderVisionStatus(status);
    log(status.message || '视觉控制已停止', 'warning');
    await pollStatus();
  } catch (error) {
    stopBrowserCamera();
    toast(error.message, 'error');
    await pollVisionStatus();
  }
}

function renderCalibration() {
  const calibration = state.calibration || {};
  const completed = new Set(calibration.completed_stages || []);
  const active = calibration.active_stage || null;
  document.querySelectorAll('[data-calibration-row]').forEach((row) => {
    const stage = row.dataset.calibrationRow;
    row.classList.toggle('completed', completed.has(stage));
    row.classList.toggle('active', active === stage);
    const button = row.querySelector('button');
    button.textContent = completed.has(stage) ? '重新采集' : active === stage ? '采集中' : '采集';
  });
  const completedCount = completed.size;
  $('calibrationProgress').value = completedCount;
  $('calibrationProgressText').textContent = active
    ? `${calibration.sample_count || 0} / ${calibration.sample_target || 24} 帧`
    : `${completedCount} / 4`;
  $('calibrationSummary').textContent = active ? '保持当前姿势'
    : calibration.profile_available ? '标定已应用' : completedCount ? '继续完成标定' : '使用默认标定';
  $('calibrationError').textContent = calibration.error || '';
  $('resetCalibrationButton').disabled = Boolean(active) || (!calibration.profile_available && !completedCount);
}

function openCalibration() {
  renderCalibration();
  $('calibrationDialog').showModal();
}

function closeCalibration() {
  $('calibrationDialog').close();
}

async function captureCalibration(stage) {
  try {
    state.calibration = await request('/api/vision/calibration/capture', {
      stage,
      sample_count: state.calibration.sample_target || 24,
    });
    renderCalibration();
    updateControlAvailability();
    log(`开始采集视觉标定：${stage}`);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function resetCalibration() {
  if (!window.confirm('确定恢复当前设备和安装侧的默认视觉标定吗？')) return;
  try {
    state.calibration = await request('/api/vision/calibration/reset', {
      device_id: state.device.id,
      side: $('visionSideSelect').value,
    });
    renderCalibration();
    updateControlAvailability();
    log('视觉标定已恢复默认', 'warning');
    toast('已恢复默认视觉标定');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function toggleVisionFullscreen() {
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else {
      await $('visionPanel').requestFullscreen();
    }
  } catch (error) {
    toast(`无法切换全屏：${error.message}`, 'error');
  }
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
    await loadPoses();
    state.enabledSides = ['right'];
    ensureSide('right');
    renderSideTabs();
    setConnectionState(false);
    log('调试服务已就绪');
    await pollStatus();
    await pollVisionStatus();
  } catch (error) {
    $('statusLabel').textContent = '服务未启动';
    $('statusDetail').textContent = error.message;
  }
}

$('deviceSelect').addEventListener('change', async (event) => {
  selectDevice(event.target.value);
  await loadPoses();
});
$('transportSelect').addEventListener('change', renderTransport);
$('connectButton').addEventListener('click', connectDevice);
$('disconnectButton').addEventListener('click', disconnectDevice);
$('sendButton').addEventListener('click', () => sendCommand());
$('stopButton').addEventListener('click', stopMotion);
$('startVisionButton').addEventListener('click', startVision);
$('stopVisionButton').addEventListener('click', stopVision);
$('fullscreenVisionButton').addEventListener('click', toggleVisionFullscreen);
$('calibrationButton').addEventListener('click', openCalibration);
$('closeCalibrationButton').addEventListener('click', closeCalibration);
$('finishCalibrationButton').addEventListener('click', closeCalibration);
$('resetCalibrationButton').addEventListener('click', resetCalibration);
document.querySelectorAll('[data-calibration-stage]').forEach((button) => {
  button.addEventListener('click', () => captureCalibration(button.dataset.calibrationStage));
});
$('calibrationDialog').addEventListener('click', (event) => {
  if (event.target === $('calibrationDialog')) closeCalibration();
});
$('visionDryRun').addEventListener('change', updateControlAvailability);
$('visionSourceSelect').addEventListener('change', renderVisionSource);
document.addEventListener('fullscreenchange', () => {
  const active = document.fullscreenElement === $('visionPanel');
  $('fullscreenVisionButton').classList.toggle('active', active);
  $('fullscreenVisionButton').title = active ? '退出全屏' : '全屏预览';
  $('fullscreenVisionButton').setAttribute('aria-label', active ? '退出全屏' : '全屏预览');
});
$('visionFrame').addEventListener('load', () => {
  state.visionFrameLoading = false;
  $('visionFrame').classList.add('visible');
  $('cameraPlaceholder').classList.add('hidden');
});
$('visionFrame').addEventListener('error', () => { state.visionFrameLoading = false; });
$('durationInput').addEventListener('input', () => { $('durationOutput').textContent = `${$('durationInput').value} ms`; });
$('clearLogButton').addEventListener('click', () => { $('activityList').innerHTML = ''; });
$('addPoseButton').addEventListener('click', () => openPoseDialog());
$('poseForm').addEventListener('submit', savePose);
$('cancelPoseButton').addEventListener('click', closePoseDialog);
$('closePoseDialogButton').addEventListener('click', closePoseDialog);
$('poseDialog').addEventListener('click', (event) => {
  if (event.target === $('poseDialog')) closePoseDialog();
});
document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault();
    sendCommand();
  }
  if (event.key === 'Escape' && state.connected) stopMotion();
});
window.addEventListener('beforeunload', stopBrowserCamera);

renderVisionSource();
initialize();
setInterval(pollStatus, 800);
setInterval(pollVisionStatus, 500);
setInterval(refreshVisionFrame, 100);
