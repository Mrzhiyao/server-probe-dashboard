const state = {
  user: null,
  requests: [],
  machines: [],
  users: [],
  page: "submit",
  kind: "temporary",
};

const els = {
  toast: document.querySelector("#toast"),
  dashboardLink: document.querySelector("#dashboardLink"),
  userBadge: document.querySelector("#userBadge"),
  logoutButton: document.querySelector("#logoutButton"),
  pageTabs: document.querySelector("#pageTabs"),
  submitPage: document.querySelector("#submitPage"),
  reviewPage: document.querySelector("#reviewPage"),
  passwordPage: document.querySelector("#passwordPage"),
  accountsPage: document.querySelector("#accountsPage"),
  temporaryForm: document.querySelector("#temporaryForm"),
  accessForm: document.querySelector("#accessForm"),
  temporaryMessage: document.querySelector("#temporaryMessage"),
  accessMessage: document.querySelector("#accessMessage"),
  ownerName: document.querySelector("#ownerName"),
  directProvisionForm: document.querySelector("#directProvisionForm"),
  directProvisionMessage: document.querySelector("#directProvisionMessage"),
  passwordForm: document.querySelector("#passwordForm"),
  passwordMessage: document.querySelector("#passwordMessage"),
  userForm: document.querySelector("#userForm"),
  userFormMessage: document.querySelector("#userFormMessage"),
  userList: document.querySelector("#userList"),
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

function requestTypeText(type) {
  return type === "access" ? "长期接入" : "临时账号";
}

function roleText(role) {
  return role === "admin" ? "管理员" : "普通用户";
}

function currentDisplayName() {
  return state.user?.display_name || state.user?.username || "";
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (response.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    throw new Error("authentication required");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return response.json();
}

function formPayload(form) {
  const data = new FormData(form);
  return Object.fromEntries(data.entries());
}

function setPage(page) {
  state.page = page;
  els.submitPage.hidden = page !== "submit";
  els.reviewPage.hidden = page !== "review";
  els.passwordPage.hidden = page !== "password";
  els.accountsPage.hidden = page !== "accounts";
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === page);
  });
}

function setKind(kind) {
  state.kind = kind;
  els.temporaryForm.hidden = kind !== "temporary";
  els.accessForm.hidden = kind !== "access";
  document.querySelectorAll(".mode-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.kind === kind);
  });
}

function machineOptions() {
  return state.machines
    .map((machine) => {
      const details = [machine.group, machine.host].filter(Boolean).join(" · ");
      const label = details ? `${machine.name} (${details})` : machine.name;
      return `<option value="${escapeHtml(machine.id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
}

function machineOptionsWithSelected(selected) {
  return state.machines
    .map((machine) => {
      const details = [machine.group, machine.host].filter(Boolean).join(" · ");
      const label = details ? `${machine.name} (${details})` : machine.name;
      return `<option value="${escapeHtml(machine.id)}"${machine.id === selected ? " selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");
}

function renderMachineSelects() {
  document.querySelectorAll("[data-machine-select]").forEach((select) => {
    const placeholder = select.querySelector("option")?.outerHTML || `<option value="">请选择机器</option>`;
    const current = select.value;
    select.innerHTML = `${placeholder}${machineOptions()}`;
    if (current) select.value = current;
  });
}

function recommendationHtml(recommendation) {
  const candidates = recommendation?.candidates || [];
  if (!candidates.length) {
    return `<div class="recommendation empty-inline">${escapeHtml(recommendation?.message || "暂无推荐")}</div>`;
  }
  const rows = candidates
    .map((item) => {
      const gpus = item.gpu_indices?.length ? `GPU ${item.gpu_indices.join(", ")}` : "CPU/内存";
      const memory =
        item.free_gpu_memory_gb === null || item.free_gpu_memory_gb === undefined ? "" : ` · 空闲显存 ${item.free_gpu_memory_gb} GB`;
      return `<div class="recommendation-row">
        <strong>${escapeHtml(item.server_name || item.server_id)}</strong>
        <span>${escapeHtml(gpus)}${memory} · CPU ${item.cpu_percent ?? "N/A"}% · 内存 ${item.memory_percent ?? "N/A"}%</span>
      </div>`;
    })
    .join("");
  return `<div class="recommendation">${rows}</div>`;
}

function existingAccountsHtml(accounts) {
  if (!accounts?.length) return "";
  return accounts
    .map((account) => {
      const machine = account.machine_label || account.machine_key || "未知机器";
      return `${escapeHtml(account.display_name || "-")} · ${escapeHtml(machine)} · ${escapeHtml(account.username)}`;
    })
    .join("<br />");
}

function isActionableAdminRequest(request) {
  return ["pending", "approved"].includes(request.status);
}

function adminControls(request) {
  if (state.user?.role !== "admin") return "";
  if (!isActionableAdminRequest(request)) return "";
  const recommended = request.recommendation?.candidates?.[0]?.server_id || "";
  const selectedMachine = request.target_machine || recommended;
  const accountType = request.request_type === "access" ? "access" : "temporary";
  const canProvision = request.status === "approved";
  const durationInput =
    accountType === "temporary"
      ? `<input name="duration_hours" type="number" min="1" step="1" value="${escapeHtml(request.duration_hours || 24)}" />`
      : `<input name="duration_hours" type="hidden" value="" />`;
  const decisionForm = `<form class="decision-form" data-id="${request.id}">
    <select name="status">
      ${["pending", "approved", "allocated", "rejected"]
        .map((status) => `<option value="${status}"${request.status === status ? " selected" : ""}>${statusText(status)}</option>`)
        .join("")}
    </select>
    <div class="decision-actions">
      <button class="button ghost decision-quick" type="button" data-status="approved">通过</button>
      <button class="button ghost danger decision-quick" type="button" data-status="rejected">拒绝</button>
    </div>
    <input name="allocation_note" placeholder="分配说明，如机器/账号/到期时间" value="${escapeHtml(request.allocation_note || "")}" />
    <input name="admin_note" placeholder="管理员备注" value="${escapeHtml(request.admin_note || "")}" />
    <button class="button" type="submit">保存</button>
  </form>`;
  if (!canProvision) return decisionForm;

  return `${decisionForm}
  <form class="provision-form" data-id="${request.id}">
    <input type="hidden" name="account_type" value="${accountType}" />
    <select name="target_machine" required>
      <option value="">选择机器</option>${machineOptionsWithSelected(selectedMachine)}
    </select>
    <input name="username" placeholder="账号名，留空自动生成" value="${escapeHtml(request.requested_account || "")}" />
    <input name="password" type="password" placeholder="密码，留空自动生成" value="${escapeHtml(request.requested_password || "")}" />
    ${durationInput}
    <button class="button" type="submit">开通账号</button>
    <p class="form-message"></p>
  </form>`;
}

function requestSecretHtml(request) {
  if (state.user?.role !== "admin" || !request.requested_password) return "";
  return `<details class="secret-box">
    <summary>查看申请密码</summary>
    <code>${escapeHtml(request.requested_password)}</code>
  </details>`;
}

function requestTitle(request) {
  if (request.request_type === "access") {
    return `长期接入：${request.requested_account || request.target_machine_label || request.target_machine}`;
  }
  return request.model_name || "临时账号申请";
}

function requestMetaHtml(request) {
  const meta = [
    requestTypeText(request.request_type),
    request.owner_name ? `姓名 ${request.owner_name}` : "",
    request.target_machine_label || request.target_machine || "",
    request.requested_account ? `账号 ${request.requested_account}` : "",
  ].filter(Boolean);
  if (request.request_type !== "access") {
    meta.push(`${request.gpu_count ?? 0} GPU`);
    meta.push(`${request.gpu_memory_gb ?? 0} GB/卡`);
    meta.push(`${request.duration_hours || "未填"} 小时`);
  }
  return meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("");
}

function requestCard(request) {
  const requester = request.requester_display_name || request.requester || state.user?.username || "";
  return `<article class="request-card ${escapeHtml(request.status)}">
    <div class="request-card-head">
      <div>
        <span class="state-pill ${escapeHtml(request.status)}">${statusText(request.status)}</span>
        <h3>${escapeHtml(requestTitle(request))}</h3>
        <p>${escapeHtml(requester)} · ${fmtTime(request.created_at)}</p>
      </div>
      <strong>${requestTypeText(request.request_type)}</strong>
    </div>
    <div class="request-meta">${requestMetaHtml(request)}</div>
    <p class="request-purpose">${escapeHtml(request.purpose)}</p>
    ${request.notes ? `<p class="request-note">${escapeHtml(request.notes)}</p>` : ""}
    ${requestSecretHtml(request)}
    ${request.request_type === "temporary" ? `<h4>资源建议</h4>${recommendationHtml(request.recommendation)}` : ""}
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
  els.requestListTitle.textContent = state.user?.role === "admin" ? "申请审批" : "我的申请";
  const visibleRequests =
    state.user?.role === "admin" ? state.requests.filter(isActionableAdminRequest) : state.requests;
  const emptyText = state.user?.role === "admin" ? "暂无待处理申请" : "暂无申请";
  els.requestList.innerHTML = visibleRequests.length ? visibleRequests.map(requestCard).join("") : `<div class="empty">${emptyText}</div>`;
  document.querySelectorAll(".decision-form").forEach((form) => {
    const saveDecision = async () => {
      const payload = formPayload(form);
      await fetchJson(`/api/resource-requests/${form.dataset.id}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await loadRequests();
    };
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await saveDecision();
    });
    form.querySelectorAll(".decision-quick").forEach((button) => {
      button.addEventListener("click", async () => {
        form.elements.status.value = button.dataset.status;
        await saveDecision();
      });
    });
  });
  document.querySelectorAll(".provision-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = form.querySelector(".form-message");
      showMessage(message, "");
      try {
        const payload = formPayload(form);
        const result = await fetchJson(`/api/resource-requests/${form.dataset.id}/provision`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const account = result.request?.allocation_note || "账号已开通";
        showMessage(message, escapeHtml(account).replaceAll("\n", "<br />"));
        showToast("账号已开通，网页登录用户已同步");
        await loadRequests();
        await loadUsers();
      } catch (error) {
        showMessage(message, escapeHtml(error.message || "开通失败"), true);
        showToast(error.message || "开通失败", true);
      }
    });
  });
}

function renderUsers(users) {
  state.users = users;
  renderPasswordUsers();
  els.userList.innerHTML = users.length
    ? users
        .map((user) => {
          const display = user.display_name ? ` · ${escapeHtml(user.display_name)}` : "";
          const status = user.is_active ? "启用" : "停用";
          const canView = user.can_view_dashboard || user.role === "admin";
          const adminLocked = user.role === "admin";
          return `<form class="user-row permission-form" data-username="${escapeHtml(user.username)}">
            <div>
              <strong>${escapeHtml(user.username)}</strong>
              <span>${roleText(user.role)}${display} · ${status}</span>
            </div>
            <label class="checkbox-line permission-control">
              <input name="can_view_dashboard" type="checkbox"${canView ? " checked" : ""}${adminLocked ? " disabled" : ""} />
              监控面板
            </label>
            <button class="button ghost mini-action" type="submit"${adminLocked ? " disabled" : ""}>保存</button>
          </form>`;
        })
        .join("")
    : `<div class="empty">暂无用户</div>`;
  document.querySelectorAll(".permission-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("button");
      const checkbox = form.elements.can_view_dashboard;
      button.disabled = true;
      button.textContent = "保存中";
      try {
        await fetchJson(`/api/users/${encodeURIComponent(form.dataset.username)}/permissions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ can_view_dashboard: checkbox.checked }),
        });
        showToast("用户权限已保存");
        await loadUsers();
      } catch (error) {
        showToast(error.message || "权限保存失败", true);
      } finally {
        button.disabled = false;
        button.textContent = "保存";
      }
    });
  });
}

function renderPasswordUsers() {
  const select = els.passwordForm.elements.username;
  select.innerHTML = state.users
    .map((user) => {
      const label = user.display_name ? `${user.username} · ${user.display_name}` : user.username;
      return `<option value="${escapeHtml(user.username)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  if (state.user?.role === "admin" && state.user.username) {
    select.value = state.user.username;
  }
}

async function loadRequests() {
  const payload = await fetchJson("/api/resource-requests");
  state.requests = payload.requests || [];
  renderRequests();
}

async function loadMachines() {
  const payload = await fetchJson("/api/request-machines");
  state.machines = payload.machines || [];
  renderMachineSelects();
}

async function loadUsers() {
  if (state.user?.role !== "admin") return;
  const payload = await fetchJson("/api/users");
  renderUsers(payload.users || []);
}

function showMessage(element, message, isError = false) {
  element.innerHTML = message;
  element.classList.toggle("error", isError);
}

function showToast(message, isError = false) {
  els.toast.innerHTML = escapeHtml(message).replaceAll("\n", "<br />");
  els.toast.classList.toggle("error", isError);
  els.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.hidden = true;
  }, isError ? 8000 : 5200);
}

function syncDirectProvisionDuration() {
  const type = els.directProvisionForm.elements.account_type.value;
  const field = els.directProvisionForm.querySelector("[data-duration-field]");
  const input = els.directProvisionForm.elements.duration_hours;
  const isTemporary = type === "temporary";
  field.hidden = !isTemporary;
  input.disabled = !isTemporary;
  input.required = isTemporary;
  if (isTemporary && !input.value) input.value = "24";
  if (!isTemporary) input.value = "";
}

function syncPasswordFormRole() {
  const isAdmin = state.user?.role === "admin";
  const adminField = els.passwordForm.querySelector("[data-password-admin]");
  const currentField = els.passwordForm.querySelector("[data-password-current]");
  const username = els.passwordForm.elements.username;
  const currentPassword = els.passwordForm.elements.current_password;
  adminField.hidden = !isAdmin;
  username.disabled = !isAdmin;
  username.required = isAdmin;
  currentField.hidden = isAdmin;
  currentPassword.disabled = isAdmin;
  currentPassword.required = !isAdmin;
}

function passwordResultMessage(result) {
  const summary = result.machine_sync_summary || {};
  const lines = ["密码已更新"];
  if (summary.total) {
    lines.push(`机器同步：成功 ${summary.ok || 0} / 失败 ${summary.failed || 0} / 跳过 ${summary.skipped || 0}`);
    (result.machine_sync || []).forEach((item) => {
      if (item.status !== "ok") {
        lines.push(`${item.machine_label || item.machine_key}: ${item.message || item.status}`);
      }
    });
  } else {
    lines.push("未找到同名机器账号，已只更新网页登录密码");
  }
  if (result.reauth_required) {
    lines.push("用户信息已更新，请重新登录");
  }
  return escapeHtml(lines.join("\n")).replaceAll("\n", "<br />");
}

function userCreateResultMessage(result) {
  const summary = result.machine_sync_summary || {};
  const lines = ["用户已创建或重置"];
  if (summary.total) {
    lines.push(`机器密码同步：成功 ${summary.ok || 0} / 失败 ${summary.failed || 0} / 跳过 ${summary.skipped || 0}`);
    (result.machine_sync || []).forEach((item) => {
      if (item.status !== "ok") {
        lines.push(`${item.machine_label || item.machine_key}: ${item.message || item.status}`);
      }
    });
  } else {
    lines.push("未找到同名机器账号，已只更新网页登录用户");
  }
  return escapeHtml(lines.join("\n")).replaceAll("\n", "<br />");
}

async function submitRequest(form, requestType, messageElement) {
  showMessage(messageElement, "");
  const payload = { ...formPayload(form), request_type: requestType };
  try {
    await fetchJson("/api/resource-requests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    form.reset();
    if (els.ownerName) els.ownerName.value = currentDisplayName();
    showMessage(messageElement, "已提交");
    await loadRequests();
    setPage("review");
  } catch (error) {
    if (error.status === 409 && error.payload?.existing_accounts?.length) {
      showMessage(messageElement, `已存在匹配账号：<br />${existingAccountsHtml(error.payload.existing_accounts)}`, true);
      return;
    }
    showMessage(messageElement, escapeHtml(error.message || "提交失败"), true);
  }
}

async function start() {
  const me = await fetchJson("/api/auth/me");
  state.user = me.user;
  const name = state.user.display_name ? `${state.user.display_name} · ${state.user.username}` : state.user.username;
  els.userBadge.textContent = `${name} · ${roleText(state.user.role)}`;
  if (els.ownerName) els.ownerName.value = currentDisplayName();
  await loadMachines();
  await loadRequests();

  if (state.user.role === "admin") {
    document.querySelectorAll("[data-admin-only]").forEach((element) => {
      element.hidden = false;
    });
    document.querySelectorAll("[data-user-only]").forEach((element) => {
      element.hidden = true;
    });
    document.querySelector('[data-page="review"]').textContent = "申请审批";
    await loadUsers();
    syncPasswordFormRole();
    setPage("review");
  } else {
    els.dashboardLink.hidden = !state.user.can_view_dashboard;
    syncPasswordFormRole();
    setPage("submit");
  }
}

els.pageTabs.addEventListener("click", (event) => {
  const button = event.target.closest(".tab-button");
  if (!button || button.hidden) return;
  setPage(button.dataset.page);
});

document.querySelectorAll(".mode-button").forEach((button) => {
  button.addEventListener("click", () => setKind(button.dataset.kind));
});

els.temporaryForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitRequest(els.temporaryForm, "temporary", els.temporaryMessage);
});

els.accessForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitRequest(els.accessForm, "access", els.accessMessage);
});

els.userForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage(els.userFormMessage, "");
  try {
    const payload = formPayload(els.userForm);
    const result = await fetchJson("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    els.userForm.reset();
    showMessage(els.userFormMessage, userCreateResultMessage(result));
    showToast("用户已创建或重置，列表已刷新");
    await loadUsers();
  } catch (error) {
    showMessage(els.userFormMessage, escapeHtml(error.message || "操作失败"), true);
    showToast(error.message || "操作失败", true);
  }
});

els.directProvisionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage(els.directProvisionMessage, "");
  try {
    const payload = formPayload(els.directProvisionForm);
    const result = await fetchJson("/api/provision-account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const account = result.account || {};
    const lines = [
      `机器：${account.machine || ""}`,
      `账号：${account.username || ""}`,
      `密码：${account.password || ""}`,
      account.expires_at ? `到期：${account.expires_at}` : "",
      "网页登录用户已同步",
    ].filter(Boolean);
    showMessage(els.directProvisionMessage, escapeHtml(lines.join("\n")).replaceAll("\n", "<br />"));
    showToast("机器账号已创建，网页登录用户已同步");
    els.directProvisionForm.reset();
    syncDirectProvisionDuration();
    await loadUsers();
  } catch (error) {
    showMessage(els.directProvisionMessage, escapeHtml(error.message || "创建失败"), true);
    showToast(error.message || "创建失败", true);
  }
});

els.directProvisionForm.elements.account_type.addEventListener("change", syncDirectProvisionDuration);
els.passwordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage(els.passwordMessage, "");
  const form = els.passwordForm;
  const newPassword = form.elements.new_password.value;
  const confirmPassword = form.elements.confirm_password.value;
  if (newPassword !== confirmPassword) {
    showMessage(els.passwordMessage, "两次输入的新密码不一致", true);
    return;
  }
  const payload = {
    new_password: newPassword,
    sync_machine_password: form.elements.sync_machine_password.checked,
  };
  if (state.user?.role === "admin") {
    payload.username = form.elements.username.value;
  } else {
    payload.current_password = form.elements.current_password.value;
  }
  try {
    const result = await fetchJson("/api/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    form.reset();
    syncPasswordFormRole();
    showMessage(els.passwordMessage, passwordResultMessage(result));
    if (result.reauth_required) {
      showToast("用户信息已更新，请重新登录");
      window.setTimeout(() => {
        window.location.href = "/login";
      }, 1400);
      return;
    }
    showToast("密码已更新");
    await loadUsers();
  } catch (error) {
    showMessage(els.passwordMessage, escapeHtml(error.message || "更新失败"), true);
    showToast(error.message || "更新失败", true);
  }
});
els.reloadButton.addEventListener("click", loadRequests);
els.logoutButton.addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST", cache: "no-store" }).catch(() => {});
  window.location.href = "/login";
});

setKind("temporary");
syncDirectProvisionDuration();
start().catch((error) => {
  els.requestList.innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
});
