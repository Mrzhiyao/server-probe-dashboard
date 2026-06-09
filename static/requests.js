const state = {
  user: null,
  requests: [],
};

const els = {
  dashboardLink: document.querySelector("#dashboardLink"),
  userBadge: document.querySelector("#userBadge"),
  logoutButton: document.querySelector("#logoutButton"),
  userAdminPanel: document.querySelector("#userAdminPanel"),
  userForm: document.querySelector("#userForm"),
  userFormMessage: document.querySelector("#userFormMessage"),
  userList: document.querySelector("#userList"),
  requestForm: document.querySelector("#requestForm"),
  requestFormMessage: document.querySelector("#requestFormMessage"),
  requestList: document.querySelector("#requestList"),
  requestListTitle: document.querySelector("#requestListTitle"),
  reloadButton: document.querySelector("#reloadButton"),
  pendingCount: document.querySelector("#pendingCount"),
  approvedCount: document.querySelector("#approvedCount"),
  rejectedCount: document.querySelector("#rejectedCount"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function statusText(status) {
  return {
    pending: "待处理",
    approved: "已通过",
    rejected: "已拒绝",
    allocated: "已分配",
  }[status] || status;
}

function roleText(role) {
  return role === "admin" ? "管理员" : "普通用户";
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (response.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    throw new Error("authentication required");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return response.json();
}

function formPayload(form) {
  const data = new FormData(form);
  return Object.fromEntries(data.entries());
}

function recommendationHtml(recommendation) {
  const candidates = recommendation?.candidates || [];
  if (!candidates.length) return `<div class="recommendation empty-inline">${escapeHtml(recommendation?.message || "暂无推荐")}</div>`;
  const rows = candidates
    .map((item) => {
      const gpus = item.gpu_indices?.length ? `GPU ${item.gpu_indices.join(", ")}` : "CPU/内存";
      const memory = item.free_gpu_memory_gb === null || item.free_gpu_memory_gb === undefined ? "" : ` · 空闲显存 ${item.free_gpu_memory_gb} GB`;
      return `<div class="recommendation-row">
        <strong>${escapeHtml(item.server_name || item.server_id)}</strong>
        <span>${escapeHtml(gpus)}${memory} · CPU ${item.cpu_percent ?? "N/A"}% · 内存 ${item.memory_percent ?? "N/A"}%</span>
      </div>`;
    })
    .join("");
  return `<div class="recommendation">${rows}</div>`;
}

function adminControls(request) {
  if (state.user?.role !== "admin") return "";
  return `<form class="decision-form" data-id="${request.id}">
    <select name="status">
      ${["pending", "approved", "allocated", "rejected"]
        .map((status) => `<option value="${status}"${request.status === status ? " selected" : ""}>${statusText(status)}</option>`)
        .join("")}
    </select>
    <input name="allocation_note" placeholder="分配说明，如机器/账号/到期时间" value="${escapeHtml(request.allocation_note || "")}" />
    <input name="admin_note" placeholder="管理员备注" value="${escapeHtml(request.admin_note || "")}" />
    <button class="button" type="submit">保存</button>
  </form>`;
}

function requestCard(request) {
  return `<article class="request-card ${escapeHtml(request.status)}">
    <div class="request-card-head">
      <div>
        <span class="state-pill ${escapeHtml(request.status)}">${statusText(request.status)}</span>
        <h3>${escapeHtml(request.model_name)}</h3>
        <p>${escapeHtml(request.requester || state.user?.username || "")} · ${fmtTime(request.created_at)}</p>
      </div>
      <strong>${escapeHtml(request.access_type || "ssh")}</strong>
    </div>
    <div class="request-meta">
      <span>${escapeHtml(request.model_size || "规模未填")}</span>
      <span>${request.gpu_count ?? 0} GPU</span>
      <span>${request.gpu_memory_gb ?? 0} GB/卡</span>
      <span>${request.duration_hours || "未填"} 小时</span>
    </div>
    <p class="request-purpose">${escapeHtml(request.purpose)}</p>
    ${request.notes ? `<p class="request-note">${escapeHtml(request.notes)}</p>` : ""}
    <h4>资源建议</h4>
    ${recommendationHtml(request.recommendation)}
    ${request.admin_note ? `<p class="request-note"><b>管理员备注：</b>${escapeHtml(request.admin_note)}</p>` : ""}
    ${request.allocation_note ? `<p class="request-note"><b>分配说明：</b>${escapeHtml(request.allocation_note)}</p>` : ""}
    ${adminControls(request)}
  </article>`;
}

function renderRequests() {
  const counts = state.requests.reduce(
    (acc, item) => {
      if (item.status === "pending") acc.pending += 1;
      else if (item.status === "rejected") acc.rejected += 1;
      else acc.approved += 1;
      return acc;
    },
    { pending: 0, approved: 0, rejected: 0 }
  );
  els.pendingCount.textContent = counts.pending;
  els.approvedCount.textContent = counts.approved;
  els.rejectedCount.textContent = counts.rejected;
  els.requestListTitle.textContent = state.user?.role === "admin" ? "全部申请" : "我的申请";
  els.requestList.innerHTML = state.requests.length
    ? state.requests.map(requestCard).join("")
    : `<div class="empty">暂无申请</div>`;
  document.querySelectorAll(".decision-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = formPayload(form);
      await fetchJson(`/api/resource-requests/${form.dataset.id}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await loadRequests();
    });
  });
}

async function loadRequests() {
  const payload = await fetchJson("/api/resource-requests");
  state.requests = payload.requests || [];
  renderRequests();
}

async function loadUsers() {
  if (state.user?.role !== "admin") return;
  const payload = await fetchJson("/api/users");
  els.userList.innerHTML = (payload.users || [])
    .map((user) => `<div><strong>${escapeHtml(user.username)}</strong><span>${roleText(user.role)} · ${user.is_active ? "启用" : "停用"}</span></div>`)
    .join("");
}

async function start() {
  const me = await fetchJson("/api/auth/me");
  state.user = me.user;
  els.userBadge.textContent = `${state.user.username} · ${roleText(state.user.role)}`;
  if (state.user.role !== "admin") {
    els.dashboardLink.hidden = true;
  } else {
    els.userAdminPanel.hidden = false;
    await loadUsers();
  }
  await loadRequests();
}

els.requestForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.requestFormMessage.textContent = "";
  const payload = formPayload(els.requestForm);
  await fetchJson("/api/resource-requests", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  els.requestForm.reset();
  els.requestFormMessage.textContent = "已提交";
  await loadRequests();
});

els.userForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.userFormMessage.textContent = "";
  const payload = formPayload(els.userForm);
  await fetchJson("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  els.userForm.reset();
  els.userFormMessage.textContent = "用户已创建或重置";
  await loadUsers();
});

els.reloadButton.addEventListener("click", loadRequests);
els.logoutButton.addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST", cache: "no-store" }).catch(() => {});
  window.location.href = "/login";
});

start().catch((error) => {
  els.requestList.innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
});
