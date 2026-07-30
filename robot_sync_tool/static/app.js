const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const scanBtn = document.getElementById("scanBtn");
const saveConfigBtn = document.getElementById("saveConfigBtn");
const testConnectionBtn = document.getElementById("testConnectionBtn");
const togglePasswordBtn = document.getElementById("togglePasswordBtn");
const statusPill = document.getElementById("statusPill");
const statusText = document.getElementById("statusText");
const notice = document.getElementById("notice");
const log = document.getElementById("log");
const queue = document.getElementById("queue");
const pageTitle = document.getElementById("pageTitle");
const pageDesc = document.getElementById("pageDesc");
let configDirty = false;
let noticeTimer = null;
let latestRecords = [];
let recordsPage = 1;
const recordsPageSize = 20;

function filteredRecordsForCurrentView() {
  const filter = document.getElementById("recordStatusFilter").value;
  return latestRecords.filter((item) => !filter || item.status === filter);
}

function totalRecordPages() {
  return Math.max(1, Math.ceil(filteredRecordsForCurrentView().length / recordsPageSize));
}

function syncQueueColumnHeight() {
  const controlPanel = document.getElementById("controlPanel");
  const queueColumn = document.getElementById("queueColumn");
  const queuePanel = document.getElementById("queuePanel");
  const queueList = document.getElementById("queue");
  const stats = queueColumn ? queueColumn.querySelector(".stats") : null;
  const queueHeader = queuePanel ? queuePanel.querySelector(".panel-header") : null;
  const queueBody = queuePanel ? queuePanel.querySelector(".panel-body") : null;
  if (!controlPanel || !queueColumn || window.innerWidth <= 980) {
    if (queueColumn) {
      queueColumn.style.height = "";
      queueColumn.style.maxHeight = "";
      queueColumn.style.removeProperty("--sync-panel-height");
    }
    if (queueList) {
      queueList.style.height = "";
      queueList.style.maxHeight = "";
    }
    return;
  }
  const controlHeight = controlPanel.getBoundingClientRect().height;
  const height = `${controlHeight}px`;
  const statsHeight = stats ? stats.getBoundingClientRect().height : 0;
  const headerHeight = queueHeader ? queueHeader.getBoundingClientRect().height : 0;
  const bodyStyle = queueBody ? window.getComputedStyle(queueBody) : null;
  const bodyPadding = bodyStyle ? parseFloat(bodyStyle.paddingTop) + parseFloat(bodyStyle.paddingBottom) : 36;
  const queueHeight = Math.max(180, controlHeight - statsHeight - headerHeight - bodyPadding - 18);
  queueColumn.style.setProperty("--sync-panel-height", height);
  queueColumn.style.height = height;
  queueColumn.style.maxHeight = height;
  if (queueList) {
    queueList.style.height = `${queueHeight}px`;
    queueList.style.maxHeight = `${queueHeight}px`;
  }
}

const pageCopy = {
  control: {
    title: "数据同步任务",
    desc: "同步 ~/data/datasets/robot，跳过压缩文件和 bak 备份数据。",
  },
  records: {
    title: "同步记录",
    desc: "查看已同步、等待稳定、同步中和失败重试的记录。",
  },
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = payload.detail || payload.error || detail;
    } catch {
      // Keep the HTTP status when the response body is not JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

function showNotice(type, message) {
  notice.textContent = message;
  notice.className = `notice visible ${type}`;
  window.clearTimeout(noticeTimer);
  noticeTimer = window.setTimeout(() => {
    notice.className = "notice";
  }, 3600);
}

function setButtonBusy(button, busyText, callback) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = busyText;
  return Promise.resolve()
    .then(callback)
    .finally(() => {
      button.textContent = original;
    });
}

function renderStatus(data) {
  statusText.textContent = data.statusText;
  statusPill.classList.toggle("running", data.running);
  startBtn.disabled = data.running;
  stopBtn.disabled = !data.running;
  const connectionOk = Boolean(data.connection && data.connection.ok);
  startBtn.disabled = data.running || !connectionOk;
  scanBtn.disabled = data.running || !connectionOk;
  testConnectionBtn.disabled = data.running;

  document.getElementById("remoteHost").textContent = data.config.remote_host;
  document.getElementById("jumpHost").textContent = data.config.jump_host || "无";
  if (!configDirty) {
    document.getElementById("remoteHostInput").value = data.config.remote_host;
    document.getElementById("jumpHostInput").value = data.config.jump_host || "";
    document.getElementById("remoteDir").value = data.config.remote_dir;
    document.getElementById("localDir").value = data.config.local_dir;
    document.getElementById("intervalSeconds").value = data.config.interval_seconds;
    document.getElementById("settleSeconds").value = data.config.settle_seconds;
    document.getElementById("recordDepth").value = data.config.record_depth;
    document.getElementById("concurrentSyncs").value = data.config.concurrent_syncs || 1;
    document.getElementById("stopMode").value = data.config.graceful_stop ? "graceful" : "immediate";
    document.getElementById("excludeListInput").value = data.config.excludes.join("\n");
    document.getElementById("sshPasswordInput").placeholder = data.config.ssh_password ? "已配置；留空表示不修改" : "留空表示使用 SSH key";
  }
  saveConfigBtn.disabled = data.running;
  document.getElementById("connectionStatus").textContent = `连接状态：${data.connection?.message || "未测试"}${data.connection?.checkedAt ? `（${data.connection.checkedAt}）` : ""}`;

  document.getElementById("syncedCount").textContent = data.syncedCount;
  document.getElementById("newCount").textContent = data.newCount;
  document.getElementById("waitingCount").textContent = data.waitingCount;
  document.getElementById("failedCount").textContent = data.failedCount;
  document.getElementById("lastScan").textContent = `上次扫描 ${data.lastScanTime || "-"}`;

  queue.innerHTML = data.queue.map((item) => `
    <div class="record-row">
      <div>
        <div class="record-name">${item.path}</div>
        <div class="record-meta">${item.meta}</div>
      </div>
      <span class="badge ${item.status}">${item.statusText}</span>
    </div>
  `).join("") || `<div class="queue-empty">暂无未同步记录</div>`;

  latestRecords = data.records;
  renderRecordsTable();

  log.innerHTML = data.logs.map((item) => `
    <div class="log-line">
      <span class="time">${item.time}</span>
      <span class="${item.level}">${item.level}</span>
      ${item.message}
    </div>
  `).join("");
  log.scrollTop = log.scrollHeight;
  syncQueueColumnHeight();
}

function renderRecordsTable() {
  const filtered = filteredRecordsForCurrentView();
  const totalPages = Math.max(1, Math.ceil(filtered.length / recordsPageSize));
  recordsPage = Math.min(Math.max(1, recordsPage), totalPages);
  const start = (recordsPage - 1) * recordsPageSize;
  const pageItems = filtered.slice(start, start + recordsPageSize);
  const recordsTable = document.getElementById("recordsTable");
  recordsTable.innerHTML = pageItems.map((item) => `
    <tr>
      <td>${item.path}</td>
      <td><span class="badge ${item.status}">${item.statusText}</span></td>
      <td>${item.sizeText}</td>
      <td>${item.fileCount}</td>
      <td>${item.lastSyncAt}</td>
    </tr>
  `).join("") || `<tr><td colspan="5">暂无同步记录</td></tr>`;
  document.getElementById("recordsPageInfo").textContent = `第 ${recordsPage} / ${totalPages} 页，共 ${filtered.length} 条`;
  document.getElementById("prevRecordsPage").disabled = recordsPage <= 1;
  document.getElementById("nextRecordsPage").disabled = recordsPage >= totalPages;
  const jumpInput = document.getElementById("recordsPageJump");
  if (jumpInput) {
    jumpInput.max = String(totalPages);
    if (document.activeElement !== jumpInput) {
      jumpInput.value = String(recordsPage);
    }
  }
}

async function refresh() {
  renderStatus(await api("/api/status"));
}

startBtn.addEventListener("click", async () => {
  try {
    await setButtonBusy(startBtn, "启动中...", async () => {
      renderStatus(await api("/api/start", { method: "POST" }));
    });
    showNotice("success", "同步任务已启动");
  } catch (error) {
    showNotice("error", `启动失败：${error.message}`);
  }
});

stopBtn.addEventListener("click", async () => {
  renderStatus(await api("/api/stop", { method: "POST" }));
});

scanBtn.addEventListener("click", async () => {
  try {
    await setButtonBusy(scanBtn, "扫描中...", async () => {
      renderStatus(await api("/api/scan", { method: "POST" }));
    });
    showNotice("success", "扫描完成");
  } catch (error) {
    showNotice("error", `扫描失败：${error.message}`);
  }
});

testConnectionBtn.addEventListener("click", async () => {
  try {
    await setButtonBusy(testConnectionBtn, "测试中...", async () => {
      renderStatus(await api("/api/test-connection", { method: "POST" }));
    });
    showNotice("success", "连接测试通过，可以开始扫描或同步");
  } catch (error) {
    renderStatus(await api("/api/status"));
    showNotice("error", `连接失败：${error.message}`);
  }
});

togglePasswordBtn.addEventListener("click", () => {
  const input = document.getElementById("sshPasswordInput");
  const visible = input.type === "text";
  input.type = visible ? "password" : "text";
  togglePasswordBtn.classList.toggle("visible", !visible);
  togglePasswordBtn.title = visible ? "显示密码" : "隐藏密码";
  togglePasswordBtn.setAttribute("aria-label", togglePasswordBtn.title);
});

saveConfigBtn.addEventListener("click", async () => {
  try {
    const config = {
      remote_host: document.getElementById("remoteHostInput").value.trim(),
      jump_host: document.getElementById("jumpHostInput").value.trim(),
      remote_dir: document.getElementById("remoteDir").value.trim(),
      local_dir: document.getElementById("localDir").value.trim(),
      interval_seconds: Number(document.getElementById("intervalSeconds").value),
      settle_seconds: Number(document.getElementById("settleSeconds").value),
      record_depth: Number(document.getElementById("recordDepth").value),
      concurrent_syncs: Number(document.getElementById("concurrentSyncs").value),
      graceful_stop: document.getElementById("stopMode").value === "graceful",
      excludes: document.getElementById("excludeListInput").value.split("\n").map((item) => item.trim()).filter(Boolean),
      ssh_password: document.getElementById("sshPasswordInput").value || "__KEEP__",
    };
    const data = await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    });
    configDirty = false;
    document.getElementById("sshPasswordInput").value = "";
    renderStatus(data);
    showNotice("success", "配置已保存，请先测试连接");
  } catch (error) {
    showNotice("error", `保存失败：${error.message}`);
  }
});

[
  "remoteHostInput",
  "jumpHostInput",
  "remoteDir",
  "localDir",
  "intervalSeconds",
  "settleSeconds",
  "recordDepth",
  "concurrentSyncs",
  "stopMode",
  "excludeListInput",
  "sshPasswordInput",
].forEach((id) => {
  document.getElementById(id).addEventListener("input", () => {
    configDirty = true;
  });
  document.getElementById(id).addEventListener("change", () => {
    configDirty = true;
  });
});

document.getElementById("refreshRecordsBtn").addEventListener("click", refresh);
document.getElementById("recordStatusFilter").addEventListener("change", () => {
  recordsPage = 1;
  renderRecordsTable();
});
document.getElementById("prevRecordsPage").addEventListener("click", () => {
  recordsPage -= 1;
  renderRecordsTable();
});
document.getElementById("nextRecordsPage").addEventListener("click", () => {
  recordsPage += 1;
  renderRecordsTable();
});
document.getElementById("jumpRecordsPage").addEventListener("click", () => {
  const jumpInput = document.getElementById("recordsPageJump");
  const target = Number(jumpInput.value || 1);
  recordsPage = Math.min(Math.max(1, target), totalRecordPages());
  renderRecordsTable();
});
document.getElementById("recordsPageJump").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") {
    return;
  }
  event.preventDefault();
  document.getElementById("jumpRecordsPage").click();
});

document.querySelectorAll(".nav button[data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    const tab = button.dataset.tab;
    document.querySelectorAll(".nav button[data-tab]").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    document.querySelectorAll(".tab-page").forEach((page) => {
      page.classList.toggle("active", page.dataset.page === tab);
    });
    pageTitle.textContent = pageCopy[tab].title;
    pageDesc.textContent = pageCopy[tab].desc;
  });
});

refresh();
setInterval(refresh, 3000);
window.addEventListener("resize", syncQueueColumnHeight);
if (window.ResizeObserver) {
  const observer = new ResizeObserver(syncQueueColumnHeight);
  const controlPanel = document.getElementById("controlPanel");
  if (controlPanel) {
    observer.observe(controlPanel);
  }
}
