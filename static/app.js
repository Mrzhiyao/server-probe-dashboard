const state = {
  snapshot: null,
  servers: [],
  groups: [],
  refreshSeconds: 60,
  historyRetentionPoints: 240,
  alertThresholds: {},
  timer: null,
  clockTimer: null,
  nextRefreshAt: null,
  loading: false,
};

const els = {
  pageTitle: document.querySelector("#pageTitle"),
  beijingTime: document.querySelector("#beijingTime"),
  aoeTime: document.querySelector("#aoeTime"),
  weatherMeta: document.querySelector("#weatherMeta"),
  weatherText: document.querySelector("#weatherText"),
  onlineCount: document.querySelector("#onlineCount"),
  warnCount: document.querySelector("#warnCount"),
  offlineCount: document.querySelector("#offlineCount"),
  groupFilter: document.querySelector("#groupFilter"),
  searchInput: document.querySelector("#searchInput"),
  sortSelect: document.querySelector("#sortSelect"),
  refreshButton: document.querySelector("#refreshButton"),
  requestLink: document.querySelector("#requestLink"),
  logoutButton: document.querySelector("#logoutButton"),
  autoRefresh: document.querySelector("#autoRefresh"),
  lastUpdated: document.querySelector("#lastUpdated"),
  refreshState: document.querySelector("#refreshState"),
  alertPanel: document.querySelector("#alertPanel"),
  serverGrid: document.querySelector("#serverGrid"),
  template: document.querySelector("#serverCardTemplate"),
};

const WEATHER_CACHE_KEY = "serverProbe.weather.beijing.v1";
const WEATHER_CACHE_MS = 15 * 60 * 1000;
const WEATHER_URL =
  "https://api.open-meteo.com/v1/forecast?latitude=39.9042&longitude=116.4074&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m&timezone=Asia%2FShanghai&forecast_days=1";

function fmtPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
  return `${Number(value).toFixed(0)}%`;
}

function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(Number(bytes))) return "N/A";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${units[index]}`;
}

function fmtLoad(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
  return Number(value).toFixed(2);
}

function fmtTime(iso) {
  if (!iso) return "未知";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function fmtClock(date, timeZone) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function fmtClockTime(date) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function finiteNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  return Number(value);
}

function weatherCodeText(code) {
  const value = Number(code);
  if (value === 0) return "晴";
  if ([1, 2].includes(value)) return "少云";
  if (value === 3) return "阴";
  if ([45, 48].includes(value)) return "雾";
  if ([51, 53, 55, 56, 57].includes(value)) return "毛毛雨";
  if ([61, 63, 65, 66, 67].includes(value)) return "雨";
  if ([71, 73, 75, 77].includes(value)) return "雪";
  if ([80, 81, 82].includes(value)) return "阵雨";
  if ([85, 86].includes(value)) return "阵雪";
  if ([95, 96, 99].includes(value)) return "雷雨";
  return "天气";
}

function weatherSummary(payload) {
  const current = payload?.current || {};
  const temp = finiteNumber(current.temperature_2m);
  const apparent = finiteNumber(current.apparent_temperature);
  const humidity = finiteNumber(current.relative_humidity_2m);
  const wind = finiteNumber(current.wind_speed_10m);
  const parts = [
    `${weatherCodeText(current.weather_code)} ${temp === null ? "N/A" : `${temp.toFixed(0)}°C`}`,
    apparent === null ? "" : `体感 ${apparent.toFixed(0)}°C`,
    humidity === null ? "" : `湿度 ${humidity.toFixed(0)}%`,
    wind === null ? "" : `风 ${wind.toFixed(0)}km/h`,
  ].filter(Boolean);
  return parts.join(" · ");
}

function readCachedWeather() {
  try {
    const cached = JSON.parse(localStorage.getItem(WEATHER_CACHE_KEY) || "null");
    if (cached && Date.now() - cached.savedAt < WEATHER_CACHE_MS) return cached.payload;
  } catch {
    return null;
  }
  return null;
}

function writeCachedWeather(payload) {
  try {
    localStorage.setItem(WEATHER_CACHE_KEY, JSON.stringify({ savedAt: Date.now(), payload }));
  } catch {
    // Storage can be unavailable in private browsing modes.
  }
}

function renderWeather(payload, stale = false) {
  if (!els.weatherText) return;
  els.weatherText.textContent = weatherSummary(payload) || "天气暂不可用";
  if (els.weatherMeta) {
    const time = payload?.current?.time ? fmtClockTime(new Date(payload.current.time)) : "";
    els.weatherMeta.textContent = ["北京天气", stale ? "缓存" : "", time].filter(Boolean).join(" · ");
  }
}

async function loadWeather() {
  const cached = readCachedWeather();
  if (cached) renderWeather(cached, true);
  try {
    const response = await fetch(WEATHER_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    writeCachedWeather(payload);
    renderWeather(payload, false);
  } catch {
    if (!cached && els.weatherText) {
      els.weatherText.textContent = "天气暂不可用";
      if (els.weatherMeta) els.weatherMeta.textContent = "北京天气";
    }
  }
}

function renderClocks() {
  const now = new Date();
  if (els.beijingTime) els.beijingTime.textContent = fmtClock(now, "Asia/Shanghai");
  if (els.aoeTime) els.aoeTime.textContent = fmtClock(now, "Etc/GMT+12");
}

function gpuDevices(metrics) {
  return metrics?.gpu?.devices || [];
}

function maxGpuUtil(metrics) {
  const devices = gpuDevices(metrics);
  const values = devices
    .map((device) => device.utilization_percent)
    .map(finiteNumber)
    .filter((value) => value !== null);
  if (!values.length) return null;
  return Math.max(...values);
}

function maxGpuMemory(metrics) {
  const devices = gpuDevices(metrics);
  const values = devices
    .map((device) => device.memory_percent)
    .map(finiteNumber)
    .filter((value) => value !== null);
  if (!values.length) return null;
  return Math.max(...values);
}

function averageGpuUtil(metrics) {
  const values = gpuDevices(metrics)
    .map((device) => finiteNumber(device.utilization_percent))
    .filter((value) => value !== null);
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function aggregateGpuMemory(metrics) {
  const totals = gpuDevices(metrics).reduce(
    (acc, device) => {
      const used = finiteNumber(device.memory_used_bytes);
      const total = finiteNumber(device.memory_total_bytes);
      if (used !== null && total !== null && total > 0) {
        acc.used += used;
        acc.total += total;
      }
      return acc;
    },
    { used: 0, total: 0 }
  );
  if (!totals.total) return null;
  return (totals.used / totals.total) * 100;
}

function aggregateGpuCardValue(metrics) {
  const util = averageGpuUtil(metrics);
  const memory = aggregateGpuMemory(metrics);
  if (util === null && memory === null) return null;
  return Math.max(Number(util || 0), Number(memory || 0));
}

function hottestGpuValue(metrics) {
  const util = maxGpuUtil(metrics);
  const memory = maxGpuMemory(metrics);
  if (util === null && memory === null) return null;
  return Math.max(Number(util || 0), Number(memory || 0));
}

function resultAlerts(result) {
  if (Array.isArray(result.alerts)) return result.alerts;
  if (result.status !== "online") return [{ severity: "critical", kind: "offline", server_name: result.name || result.id }];
  const metrics = result.metrics || {};
  const alerts = [];
  const cpu = finiteNumber(metrics.cpu?.percent);
  const mem = finiteNumber(metrics.memory?.percent);
  const gpu = finiteNumber(hottestGpuValue(metrics));
  const disk = finiteNumber(metrics.disk?.percent);
  if (cpu !== null && cpu >= 85) alerts.push({ severity: cpu >= 95 ? "critical" : "warning", kind: "cpu", metric: "CPU", value: cpu });
  if (mem !== null && mem >= 88) alerts.push({ severity: mem >= 95 ? "critical" : "warning", kind: "memory", metric: "Memory", value: mem });
  if (gpu !== null && gpu >= 92) alerts.push({ severity: gpu >= 98 ? "critical" : "warning", kind: "gpu", metric: "GPU", value: gpu });
  if (disk !== null && disk >= 90) alerts.push({ severity: disk >= 95 ? "critical" : "warning", kind: "disk", metric: "Disk", value: disk });
  return alerts;
}

function healthClass(result) {
  if (result.status !== "online") return "offline";
  if (resultAlerts(result).some((alert) => alert.severity === "critical")) return "critical";
  if (resultAlerts(result).length) return "warning";
  return "online";
}

function dialColor(value) {
  if (value === null || value === undefined) return "#6f7782";
  if (value >= 90) return "var(--red)";
  if (value >= 75) return "var(--amber)";
  if (value >= 50) return "var(--cyan)";
  return "var(--green)";
}

function setDial(node, value) {
  const normalized = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, Number(value)));
  node.style.setProperty("--value", normalized);
  node.style.setProperty("--dial-color", dialColor(value));
  node.querySelector("span").textContent = fmtPercent(value);
}

function shortCommand(command) {
  if (!command) return "";
  return command.replace(/\s+/g, " ").trim();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function tableCell(value, className = "") {
  return `<td${className ? ` class="${className}"` : ""}>${value}</td>`;
}

function processTable(rows, type) {
  if (!rows || !rows.length) {
    const text =
      type === "gpu"
        ? "暂无 GPU 进程；如果显存只有十几 MB，通常只是驱动或空闲占用。"
        : type === "gpu-user"
        ? "暂无 GPU 用户占用"
        : "暂无进程数据";
    return `<div class="empty">${text}</div>`;
  }

  const headers =
    type === "gpu"
      ? ["GPU", "PID", "用户", "运行时长", "算力", "显存利用", "显存", "CPU", "内存", "命令"]
      : type === "gpu-user"
      ? ["用户", "进程", "GPU", "显存", "算力合计", "最高算力", "CPU", "内存"]
      : ["PID", "用户", "运行时长", "CPU", "内存", "RSS", "命令"];

  const body = rows
    .map((row) => {
      const command = escapeHtml(shortCommand(row.command || row.process_name));
      if (type === "gpu-user") {
        const gpuText = Array.isArray(row.gpu_indices) && row.gpu_indices.length ? row.gpu_indices.join(", ") : "-";
        return `<tr>
          ${tableCell(escapeHtml(row.user || "unknown"))}
          ${tableCell(row.process_count ?? 0)}
          ${tableCell(escapeHtml(gpuText))}
          ${tableCell(fmtBytes(row.used_memory_bytes))}
          ${tableCell(fmtPercent(row.gpu_sm_percent_sum))}
          ${tableCell(fmtPercent(row.gpu_sm_percent_max))}
          ${tableCell(fmtPercent(row.cpu_percent_sum))}
          ${tableCell(fmtPercent(row.mem_percent_sum))}
        </tr>`;
      }
      if (type === "gpu") {
        const gpuName = row.gpu_index !== undefined && row.gpu_index !== null ? `GPU ${row.gpu_index}` : "GPU";
        return `<tr>
          ${tableCell(escapeHtml(gpuName))}
          ${tableCell(row.pid ?? "")}
          ${tableCell(escapeHtml(row.user || ""))}
          ${tableCell(row.runtime || "")}
          ${tableCell(fmtPercent(row.gpu_sm_percent))}
          ${tableCell(fmtPercent(row.gpu_mem_percent))}
          ${tableCell(fmtBytes(row.used_memory_bytes))}
          ${tableCell(fmtPercent(row.cpu_percent))}
          ${tableCell(fmtPercent(row.mem_percent))}
          ${tableCell(command, "command")}
        </tr>`;
      }
      return `<tr>
        ${tableCell(row.pid ?? "")}
        ${tableCell(escapeHtml(row.user || ""))}
        ${tableCell(row.runtime || "")}
        ${tableCell(fmtPercent(row.cpu_percent))}
        ${tableCell(fmtPercent(row.mem_percent))}
        ${tableCell(fmtBytes(row.rss_bytes))}
        ${tableCell(command, "command")}
      </tr>`;
    })
    .join("");

  return `<table>
    <thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

function gpuDeviceLabel(device) {
  const util = fmtPercent(device.utilization_percent);
  const memoryPercent = fmtPercent(device.memory_percent);
  const memoryUsed = fmtBytes(device.memory_used_bytes);
  const memoryTotal = fmtBytes(device.memory_total_bytes);
  const temp = device.temperature_c === null || device.temperature_c === undefined ? "" : ` · ${device.temperature_c}°C`;
  const index = device.index === null || device.index === undefined ? "" : `GPU ${device.index} · `;
  return `${index}${device.name || "GPU"} · 算力 ${util} · 显存 ${memoryPercent} (${memoryUsed}/${memoryTotal})${temp}`;
}

function barValue(value) {
  const number = finiteNumber(value);
  if (number === null) return 0;
  return Math.max(0, Math.min(100, number));
}

function gpuBreakdown(metrics) {
  const devices = gpuDevices(metrics);
  if (devices.length < 2) return "";

  const cards = devices
    .map((device) => {
      const index = device.index === null || device.index === undefined ? "?" : device.index;
      const util = device.utilization_percent;
      const memory = device.memory_percent;
      const temp = device.temperature_c === null || device.temperature_c === undefined ? "N/A" : `${device.temperature_c}°C`;
      return `<div class="gpu-unit">
        <div class="gpu-mini-head">
          <strong>GPU ${escapeHtml(index)}</strong>
          <span>${escapeHtml(device.name || "GPU")}</span>
        </div>
        <div class="gpu-mini-meter">
          <div><span>算力</span><b>${fmtPercent(util)}</b></div>
          <i style="--value:${barValue(util)}"></i>
        </div>
        <div class="gpu-mini-meter">
          <div><span>显存</span><b>${fmtPercent(memory)}</b></div>
          <i style="--value:${barValue(memory)}"></i>
        </div>
        <div class="gpu-mini-foot">
          <span>${fmtBytes(device.memory_used_bytes)} / ${fmtBytes(device.memory_total_bytes)}</span>
          <span>${temp}</span>
        </div>
      </div>`;
    })
    .join("");

  return `<section class="gpu-mini-grid" aria-label="单卡 GPU 资源">${cards}</section>`;
}

function gpuSummaryLabel(metrics) {
  const devices = gpuDevices(metrics);
  if (!devices.length) return "无 GPU 数据";
  return `${devices.length}卡 · 平均算力 ${fmtPercent(averageGpuUtil(metrics))} · 总显存 ${fmtPercent(
    aggregateGpuMemory(metrics)
  )} · 最忙单卡 ${fmtPercent(hottestGpuValue(metrics))}`;
}

function alertText(alert) {
  if (alert.kind === "offline") return "离线";
  const metricName = {
    cpu: "CPU",
    memory: "内存",
    gpu: "GPU",
    disk: "根分区",
  }[alert.kind] || alert.metric || "资源";
  const value = alert.value === null || alert.value === undefined ? "" : `${fmtPercent(alert.value)}`;
  const threshold = alert.threshold === null || alert.threshold === undefined ? "" : ` / 阈值 ${fmtPercent(alert.threshold)}`;
  return `${metricName} ${value}${threshold}`;
}

function renderCardAlerts(result) {
  const alerts = resultAlerts(result);
  if (!alerts.length) return "";
  return `<div class="alert-chips">${alerts
    .slice(0, 3)
    .map((alert) => `<span class="${alert.severity === "critical" ? "critical" : "warning"}">${escapeHtml(alertText(alert))}</span>`)
    .join("")}</div>`;
}

function historySamples(result) {
  const history = state.snapshot?.history || {};
  return history[result.id] || [];
}

function pointsFor(samples, key, width, height) {
  const values = samples
    .map((sample) => finiteNumber(sample[key]))
    .map((value) => (value === null ? null : Math.max(0, Math.min(100, value))));
  const validCount = values.filter((value) => value !== null).length;
  if (validCount < 2) return "";
  const step = values.length <= 1 ? 0 : width / (values.length - 1);
  return values
    .map((value, index) => {
      if (value === null) return null;
      const x = Number(index * step).toFixed(1);
      const y = Number(height - (value / 100) * height).toFixed(1);
      return `${x},${y}`;
    })
    .filter(Boolean)
    .join(" ");
}

function historyWindowText(samples) {
  if (samples.length < 2) return "积累中";
  const first = new Date(samples[0].time);
  const last = new Date(samples[samples.length - 1].time);
  const minutes = Math.max(1, Math.round((last.getTime() - first.getTime()) / 60000));
  if (!Number.isFinite(minutes)) return `${samples.length} 点`;
  if (minutes >= 120) return `近 ${Math.round(minutes / 60)} 小时`;
  return `近 ${minutes} 分钟`;
}

function historyPanel(result) {
  const samples = historySamples(result).slice(-90);
  const hasHistory = samples.filter((sample) => sample.status === "online").length >= 2;
  const width = 260;
  const height = 62;
  const cpu = pointsFor(samples, "cpu", width, height);
  const mem = pointsFor(samples, "mem", width, height);
  const gpu = pointsFor(samples, "gpu_peak", width, height);
  return `<section class="history-card">
    <div class="history-head"><span>历史曲线</span><strong>${escapeHtml(historyWindowText(samples))}</strong></div>
    ${
      hasHistory
        ? `<svg class="sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="资源历史曲线">
            <line x1="0" y1="${height * 0.25}" x2="${width}" y2="${height * 0.25}"></line>
            <line x1="0" y1="${height * 0.5}" x2="${width}" y2="${height * 0.5}"></line>
            <line x1="0" y1="${height * 0.75}" x2="${width}" y2="${height * 0.75}"></line>
            ${cpu ? `<polyline class="cpu" points="${cpu}"></polyline>` : ""}
            ${mem ? `<polyline class="mem" points="${mem}"></polyline>` : ""}
            ${gpu ? `<polyline class="gpu" points="${gpu}"></polyline>` : ""}
          </svg>`
        : `<div class="history-empty">等待更多采样点</div>`
    }
    <div class="history-legend">
      <span class="cpu">CPU</span><span class="mem">内存</span><span class="gpu">GPU峰值</span>
    </div>
  </section>`;
}

function gpuUsersPanel(metrics) {
  const rows = metrics?.gpu?.user_summary || [];
  const devices = gpuDevices(metrics);
  if (!devices.length) return "";
  if (!rows.length) {
    return `<section class="gpu-users-card"><div class="gpu-users-head"><span>GPU 用户</span><strong>暂无活跃进程</strong></div></section>`;
  }
  const maxMemory = Math.max(...rows.map((row) => finiteNumber(row.used_memory_bytes) || 0), 1);
  const body = rows
    .slice(0, 4)
    .map((row) => {
      const memory = finiteNumber(row.used_memory_bytes) || 0;
      const width = Math.max(4, Math.min(100, (memory / maxMemory) * 100));
      const gpus = Array.isArray(row.gpu_indices) && row.gpu_indices.length ? row.gpu_indices.join(",") : "-";
      return `<div class="gpu-user-row">
        <div>
          <strong>${escapeHtml(row.user || "unknown")}</strong>
          <span>${row.process_count || 0} 进程 · GPU ${escapeHtml(gpus)}</span>
        </div>
        <b>${fmtBytes(row.used_memory_bytes)}</b>
        <i style="--value:${width}"></i>
      </div>`;
    })
    .join("");
  return `<section class="gpu-users-card">
    <div class="gpu-users-head"><span>GPU 用户</span><strong>${rows.length} 用户</strong></div>
    ${body}
  </section>`;
}

function allAlerts() {
  if (Array.isArray(state.snapshot?.alerts)) return state.snapshot.alerts;
  return (state.snapshot?.results || []).flatMap((result) => resultAlerts(result));
}

function renderAlertPanel() {
  if (!els.alertPanel) return;
  const alerts = allAlerts();
  if (!alerts.length) {
    els.alertPanel.hidden = true;
    els.alertPanel.innerHTML = "";
    return;
  }
  const critical = alerts.filter((alert) => alert.severity === "critical").length;
  const warning = alerts.length - critical;
  const rows = alerts
    .slice(0, 10)
    .map(
      (alert) => `<div class="alert-row ${alert.severity === "critical" ? "critical" : "warning"}">
        <span>${alert.severity === "critical" ? "严重" : "告警"}</span>
        <strong>${escapeHtml(alert.server_name || alert.server_id || "")}</strong>
        <p>${escapeHtml(alertText(alert))}</p>
      </div>`
    )
    .join("");
  els.alertPanel.hidden = false;
  els.alertPanel.innerHTML = `<div class="alert-panel-head">
      <h2>当前告警</h2>
      <span>${critical} 严重 · ${warning} 告警</span>
    </div>
    <div class="alert-list">${rows}</div>`;
}

function quickRows(result) {
  if (result.status !== "online") {
    return `<div class="error-box">${escapeHtml(result.error || "连接失败")}</div>`;
  }
  const metrics = result.metrics || {};
  const uptime = metrics.uptime_seconds ? secondsText(metrics.uptime_seconds) : "N/A";
  return `
    <div class="quick-row"><span>负载</span><strong>${fmtLoad(metrics.cpu?.load1)} / ${fmtLoad(metrics.cpu?.load5)} / ${fmtLoad(
    metrics.cpu?.load15
  )}</strong></div>
    <div class="quick-row"><span>GPU汇总</span><strong>${escapeHtml(gpuSummaryLabel(metrics))}</strong></div>
    <div class="quick-row"><span>根分区</span><strong>${fmtPercent(metrics.disk?.percent)} · ${fmtBytes(metrics.disk?.used_bytes)}</strong></div>
    <div class="quick-row"><span>运行</span><strong>${uptime}</strong></div>
  `;
}

function secondsText(seconds) {
  const value = Number(seconds);
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  if (days) return `${days}天 ${hours}小时`;
  if (hours) return `${hours}小时 ${minutes}分`;
  return `${minutes}分`;
}

function renderCard(result) {
  const card = els.template.content.firstElementChild.cloneNode(true);
  const status = healthClass(result);
  const metrics = result.metrics || {};
  let detailMetrics = metrics;
  let detailLoaded = Boolean(metrics.processes?.top_cpu || metrics.processes?.top_mem || metrics.gpu?.processes);
  let detailLoading = false;
  let activeProcessTab = "cpu";
  card.classList.add(status);
  card.querySelector(".group-label").textContent = result.group || "";
  card.querySelector("h2").textContent = result.name || result.id;
  card.querySelector(".host-line").textContent = `${result.user || ""}@${result.host || "local"} · ${result.latency_ms ?? 0}ms`;

  const pill = card.querySelector(".state-pill");
  pill.classList.add(status);
  pill.textContent = status === "offline" ? "离线" : status === "critical" ? "严重" : status === "warning" ? "高负载" : "在线";

  const dials = card.querySelectorAll(".dial");
  setDial(dials[0], metrics.cpu?.percent);
  setDial(dials[1], metrics.memory?.percent);
  setDial(dials[2], aggregateGpuCardValue(metrics));

  card.querySelector(".card-alerts").innerHTML = renderCardAlerts(result);
  card.querySelector(".gpu-breakdown").innerHTML = gpuBreakdown(metrics);
  card.querySelector(".history-panel").innerHTML = historyPanel(result);
  card.querySelector(".gpu-users").innerHTML = gpuUsersPanel(metrics);
  card.querySelector(".quick-lines").innerHTML = quickRows(result);

  const panel = card.querySelector(".process-panel");
  const tabs = panel.querySelectorAll(".tabs button");
  const tableWrap = panel.querySelector(".table-wrap");
  const loadDetails = async () => {
    if (detailLoaded || detailLoading || result.status !== "online") return;
    detailLoading = true;
    tableWrap.innerHTML = `<div class="empty">加载进程数据中</div>`;
    try {
      const detail = await fetchJson(`/api/server/${encodeURIComponent(result.id)}`);
      detailMetrics = detail.metrics || metrics;
      detailLoaded = true;
      updateTable(activeProcessTab);
    } catch (error) {
      tableWrap.innerHTML = `<div class="error-box">进程数据加载失败：${escapeHtml(error.message)}</div>`;
    } finally {
      detailLoading = false;
    }
  };
  const updateTable = (tab) => {
    activeProcessTab = tab;
    tabs.forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
    if (!detailLoaded && result.status === "online") {
      tableWrap.innerHTML = `<div class="empty">展开后加载进程数据</div>`;
      return;
    }
    if (tab === "mem") tableWrap.innerHTML = processTable(detailMetrics.processes?.top_mem, "mem");
    else if (tab === "gpu") tableWrap.innerHTML = processTable(detailMetrics.gpu?.processes, "gpu");
    else if (tab === "gpu-user") tableWrap.innerHTML = processTable(detailMetrics.gpu?.user_summary, "gpu-user");
    else tableWrap.innerHTML = processTable(detailMetrics.processes?.top_cpu, "cpu");
  };
  tabs.forEach((button) => button.addEventListener("click", () => updateTable(button.dataset.tab)));
  panel.addEventListener("toggle", () => {
    card.classList.toggle("expanded", panel.open);
    if (panel.open) loadDetails();
  });
  updateTable("cpu");
  return card;
}

function filteredResults() {
  const group = els.groupFilter.value;
  const search = els.searchInput.value.trim().toLowerCase();
  const sort = els.sortSelect.value;
  let results = [...(state.snapshot?.results || [])];
  if (group !== "all") results = results.filter((item) => item.group === group);
  if (search) {
    results = results.filter((item) => {
      const haystack = `${item.id} ${item.name} ${item.host} ${item.user} ${item.group}`.toLowerCase();
      return haystack.includes(search);
    });
  }
  results.sort((a, b) => {
    if (sort === "cpu") return Number(b.metrics?.cpu?.percent || -1) - Number(a.metrics?.cpu?.percent || -1);
    if (sort === "mem") return Number(b.metrics?.memory?.percent || -1) - Number(a.metrics?.memory?.percent || -1);
    if (sort === "gpu") return Number(aggregateGpuCardValue(b.metrics) || -1) - Number(aggregateGpuCardValue(a.metrics) || -1);
    if (sort === "status") return healthClass(a).localeCompare(healthClass(b));
    return `${a.group}${a.name}`.localeCompare(`${b.group}${b.name}`);
  });
  return results;
}

function machineType(result) {
  const count = gpuDevices(result.metrics || {}).length;
  if (count <= 1) return { key: "ordinary", label: "普通机器", order: 1 };
  if (count === 4) return { key: "gpu4", label: "四卡机", order: 2 };
  if (count === 8) return { key: "gpu8", label: "八卡机", order: 3 };
  return { key: `gpu${count}`, label: `${count}卡机`, order: 10 + count };
}

function machineBuckets(results) {
  const map = new Map();
  for (const result of results) {
    const type = machineType(result);
    if (!map.has(type.key)) {
      map.set(type.key, { ...type, results: [] });
    }
    map.get(type.key).results.push(result);
  }
  return [...map.values()].sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));
}

function renderCardGrid(container, results) {
  results.forEach((result) => container.appendChild(renderCard(result)));
}

function renderSection(bucket) {
  const section = document.createElement("section");
  section.className = "machine-section";
  const title = document.createElement("div");
  title.className = "machine-section-head";
  title.innerHTML = `<h3>${escapeHtml(bucket.label)}</h3><span>${bucket.results.length} 台</span>`;
  const grid = document.createElement("div");
  grid.className = "machine-section-grid";
  renderCardGrid(grid, bucket.results);
  section.appendChild(title);
  section.appendChild(grid);
  return section;
}

function render() {
  const results = filteredResults();
  const counts = (state.snapshot?.results || []).reduce(
    (acc, result) => {
      const status = healthClass(result);
      if (status === "offline") acc.offline += 1;
      else if (status === "warning" || status === "critical") acc.warn += 1;
      else acc.online += 1;
      return acc;
    },
    { online: 0, warn: 0, offline: 0 }
  );

  els.onlineCount.textContent = counts.online;
  els.warnCount.textContent = counts.warn;
  els.offlineCount.textContent = counts.offline;
  renderAlertPanel();
  els.serverGrid.innerHTML = "";
  els.serverGrid.classList.remove("sectioned");

  if (!results.length) {
    els.serverGrid.innerHTML = `<div class="empty">没有匹配的机器</div>`;
    return;
  }

  const buckets = machineBuckets(results);
  if (buckets.length <= 1) {
    renderCardGrid(els.serverGrid, results);
    return;
  }

  els.serverGrid.classList.add("sectioned");
  buckets.forEach((bucket) => els.serverGrid.appendChild(renderSection(bucket)));
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (response.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    throw new Error("authentication required");
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function loadAuthState() {
  const auth = await fetchJson("/api/auth/me");
  if (els.requestLink && auth.user) {
    els.requestLink.textContent = auth.user.role === "admin" ? "用户管理" : "申请";
  }
  if (!auth.auth_enabled) {
    if (els.requestLink) els.requestLink.hidden = true;
    if (els.logoutButton) els.logoutButton.hidden = true;
  }
}

async function loadMeta() {
  const meta = await fetchJson("/api/servers");
  state.servers = meta.servers || [];
  state.groups = meta.groups || [];
  state.refreshSeconds = Number(meta.refresh_seconds || 60);
  state.historyRetentionPoints = Number(meta.history_retention_points || 240);
  state.alertThresholds = meta.alert_thresholds || {};
  const title = meta.title || "Server Probe Dashboard";
  document.title = title;
  els.pageTitle.textContent = title;
  renderRefreshState();
  els.groupFilter.innerHTML = `<option value="all">全部</option>${state.groups
    .map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`)
    .join("")}`;
}

async function loadSnapshot(force = false) {
  if (state.loading) return;
  state.loading = true;
  els.refreshButton.disabled = true;
  els.refreshButton.textContent = "采集中";
  try {
    state.snapshot = await fetchJson(`/api/snapshot${force ? "?force=1" : ""}`);
    if (!state.snapshot.cache?.has_snapshot) {
      els.lastUpdated.textContent = "缓存初始化中";
    } else {
      const refreshHint = state.snapshot.cache?.refreshing ? " · 后台刷新中" : "";
      els.lastUpdated.textContent = `缓存 ${fmtTime(state.snapshot.generated_at)}${refreshHint}`;
    }
    render();
    if (state.snapshot.cache?.refreshing) {
      window.setTimeout(() => loadSnapshot(false), 2500);
    }
  } catch (error) {
    els.lastUpdated.textContent = `采集失败：${error.message}`;
  } finally {
    state.loading = false;
    els.refreshButton.disabled = false;
    els.refreshButton.textContent = "刷新";
  }
}

function schedule() {
  clearInterval(state.timer);
  if (!els.autoRefresh.checked) {
    state.nextRefreshAt = null;
    renderRefreshState();
    return;
  }
  state.nextRefreshAt = Date.now() + state.refreshSeconds * 1000;
  renderRefreshState();
  state.timer = setInterval(() => {
    state.nextRefreshAt = Date.now() + state.refreshSeconds * 1000;
    renderRefreshState();
    loadSnapshot(false);
  }, state.refreshSeconds * 1000);
}

function renderRefreshState() {
  if (!els.refreshState) return;
  if (!els.autoRefresh.checked) {
    els.refreshState.textContent = `自动已停 · 间隔 ${state.refreshSeconds}s`;
    return;
  }
  if (!state.nextRefreshAt) {
    els.refreshState.textContent = `间隔 ${state.refreshSeconds}s`;
    return;
  }
  const secondsLeft = Math.max(0, Math.ceil((state.nextRefreshAt - Date.now()) / 1000));
  els.refreshState.textContent = `间隔 ${state.refreshSeconds}s · 下次 ${fmtClockTime(new Date(state.nextRefreshAt))} · 还有 ${secondsLeft}s`;
}

function startUiTicker() {
  renderClocks();
  renderRefreshState();
  clearInterval(state.clockTimer);
  state.clockTimer = setInterval(() => {
    renderClocks();
    renderRefreshState();
  }, 1000);
}

els.refreshButton.addEventListener("click", () => loadSnapshot(true));
els.logoutButton?.addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST", cache: "no-store" }).catch(() => {});
  window.location.href = "/login";
});
els.autoRefresh.addEventListener("change", schedule);
els.groupFilter.addEventListener("change", render);
els.searchInput.addEventListener("input", render);
els.sortSelect.addEventListener("change", render);

(async function start() {
  startUiTicker();
  loadWeather();
  setInterval(loadWeather, WEATHER_CACHE_MS);
  await loadAuthState();
  await loadMeta();
  await loadSnapshot(false);
  schedule();
})();
