const els = {
  toast: document.querySelector("#toast"),
  personName: document.querySelector("#personName"),
  personAccounts: document.querySelector("#personAccounts"),
  usageKpis: document.querySelector("#usageKpis"),
  updatedAt: document.querySelector("#updatedAt"),
  machineList: document.querySelector("#machineList"),
  refreshButton: document.querySelector("#refreshButton"),
  logoutButton: document.querySelector("#logoutButton"),
};

const params = new URLSearchParams(window.location.search);
const personRef = params.get("person") || "";
let refreshTimer = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function fmtBytes(value) {
  let number = Math.max(0, finiteNumber(value));
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${Math.round(number)} ${units[unit]}` : `${number.toFixed(1)} ${units[unit]}`;
}

function fmtPercent(value) {
  const number = finiteNumber(value);
  return `${number.toFixed(number >= 10 ? 1 : 2).replace(/\.0+$/, "").replace(/(\.\d*[1-9])0+$/, "$1")}%`;
}

function fmtDuration(value) {
  let seconds = Math.max(0, Math.floor(finiteNumber(value)));
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}天 ${hours}小时`;
  if (hours) return `${hours}小时 ${minutes}分钟`;
  if (minutes) return `${minutes}分钟`;
  return `${seconds}秒`;
}

function usagePercent(value, total) {
  const capacity = finiteNumber(total);
  if (capacity <= 0) return 0;
  return Math.max(0, Math.min(100, (finiteNumber(value) / capacity) * 100));
}

function metric(label, value, hint, tone = "cyan") {
  return `<div class="usage-kpi ${tone}"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong><span>${escapeHtml(hint)}</span></div>`;
}

function bar(label, value, total, tone) {
  const percent = usagePercent(value, total);
  return `<div class="usage-meter ${tone}">
    <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(fmtBytes(value))} / ${escapeHtml(fmtBytes(total))}</strong></div>
    <i><b style="width:${percent}%"></b></i>
    <small>${escapeHtml(fmtPercent(percent))}</small>
  </div>`;
}

function processLabel(process) {
  if (process.container_name) {
    return `<strong>${escapeHtml(process.container_name)}</strong><span>Docker${process.model ? ` · ${escapeHtml(process.model)}` : ""}</span>`;
  }
  return `<strong>${escapeHtml(process.process_name || "unknown")}</strong><span>直接进程</span>`;
}

function processTable(processes) {
  if (!processes.length) return `<div class="usage-empty">当前没有可识别的 GPU 进程</div>`;
  const rows = processes
    .map(
      (process) => `<tr>
        <td>${escapeHtml((process.gpu_indices || []).join(",") || "-")}</td>
        <td><div class="usage-process-name">${processLabel(process)}</div></td>
        <td><strong>${escapeHtml(fmtBytes(process.used_memory_bytes))}</strong></td>
        <td>${escapeHtml(process.pid ?? "-")}</td>
        <td>${escapeHtml(fmtDuration(process.runtime_seconds))}</td>
      </tr>`,
    )
    .join("");
  return `<div class="usage-table-wrap"><table class="usage-table">
    <thead><tr><th>GPU</th><th>进程 / 容器</th><th>显存</th><th>PID</th><th>运行时长</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function containerTable(containers) {
  if (!containers.length) return "";
  const rows = containers
    .map(
      (container) => `<tr>
        <td><strong>${escapeHtml(container.name || "-")}</strong></td>
        <td>${escapeHtml(container.model || container.image || "-")}</td>
        <td>${escapeHtml((container.gpu_indices || []).join(",") || "-")}</td>
        <td>${escapeHtml(fmtBytes(container.gpu_memory_used_bytes))}</td>
        <td>${escapeHtml(fmtBytes(container.memory_used_bytes))}</td>
        <td>${escapeHtml(fmtPercent(container.cpu_percent))}</td>
      </tr>`,
    )
    .join("");
  return `<div class="usage-section-head"><h3>归属容器</h3><span>${containers.length} 个</span></div>
    <div class="usage-table-wrap"><table class="usage-table">
      <thead><tr><th>容器</th><th>镜像 / 模型</th><th>GPU</th><th>显存</th><th>内存</th><th>CPU</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

function machineCard(row) {
  const processes = Array.isArray(row.top_gpu_processes) ? row.top_gpu_processes : [];
  const containers = Array.isArray(row.containers) ? row.containers : [];
  const meta = [row.host, row.group, row.user].filter(Boolean).join(" · ");
  return `<article class="usage-machine">
    <header>
      <div><h2>${escapeHtml(row.server_name || row.server_id)}</h2><p>${escapeHtml(meta)}</p></div>
      <span>${escapeHtml((row.gpu_indices || []).length ? `GPU ${(row.gpu_indices || []).join(",")}` : "无 GPU 进程")}</span>
    </header>
    <div class="usage-machine-metrics">
      ${bar("显存", row.gpu_memory_bytes, row.gpu_memory_capacity_bytes, "violet")}
      ${bar("内存", row.memory_bytes, row.memory_total_bytes, "cyan")}
      <div class="usage-plain-metric"><span>CPU（多核）</span><strong>${escapeHtml(fmtPercent(row.cpu_percent))}</strong></div>
      <div class="usage-plain-metric"><span>进程</span><strong>${escapeHtml(row.process_count ?? 0)}</strong></div>
    </div>
    <div class="usage-section-head"><h3>GPU 进程</h3><span>按显存排序</span></div>
    ${processTable(processes)}
    ${containerTable(containers)}
  </article>`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    throw new Error("需要登录");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

async function loadUsage() {
  if (!personRef) {
    els.machineList.innerHTML = `<div class="usage-empty">链接中缺少用户信息</div>`;
    return;
  }
  els.refreshButton.disabled = true;
  try {
    const payload = await fetchJson(`/api/user-usage?person=${encodeURIComponent(personRef)}`);
    const person = payload.person || {};
    const rows = Array.isArray(person.machine_rows) ? person.machine_rows : [];
    const display = person.display_name || (person.usernames || [personRef])[0] || personRef;
    const accounts = (person.usernames || []).map((username) => `@${username}`).join(" · ");
    const totalGpu = rows.reduce((sum, row) => sum + finiteNumber(row.gpu_memory_bytes), 0);
    const totalMemory = rows.reduce((sum, row) => sum + finiteNumber(row.memory_bytes), 0);
    const containerCount = rows.reduce((sum, row) => sum + finiteNumber(row.container_count), 0);
    els.personName.textContent = display;
    document.title = `${display} - 用户资源详情`;
    els.personAccounts.textContent = `${accounts || "姓名未登记"} · ${person.machine_count || rows.length} 台机器`;
    els.usageKpis.innerHTML = [
      metric("机器", String(person.machine_count || rows.length), "当前有资源进程", "cyan"),
      metric("显存", fmtBytes(totalGpu), `${rows.filter((row) => finiteNumber(row.gpu_memory_bytes) > 0).length} 台 GPU 主机`, "violet"),
      metric("内存", fmtBytes(totalMemory), "用户进程与归属容器", "green"),
      metric("容器", String(containerCount), "可识别归属", "amber"),
    ].join("");
    els.updatedAt.textContent = `更新 ${new Date(payload.generated_at).toLocaleString("zh-CN", { hour12: false })}`;
    els.machineList.innerHTML = rows.length ? rows.map(machineCard).join("") : `<div class="usage-empty">当前没有资源记录</div>`;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(loadUsage, Math.max(30, Number(payload.refresh_seconds) || 60) * 1000);
  } catch (error) {
    els.toast.textContent = error.message || "资源详情加载失败";
    els.toast.hidden = false;
    els.machineList.innerHTML = `<div class="usage-empty">${escapeHtml(error.message || "资源详情加载失败")}</div>`;
  } finally {
    els.refreshButton.disabled = false;
  }
}

els.refreshButton.addEventListener("click", loadUsage);
els.logoutButton.addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => null);
  window.location.href = "/login";
});

loadUsage();
