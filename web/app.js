const state = { user: null, csrfToken: "", view: "submit", filter: "all", search: "" };
const $ = (selector) => document.querySelector(selector);
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function showToast(message) {
  const toast = $("#toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2800);
}

function setSession(data) {
  state.user = data.user;
  state.csrfToken = data.csrf_token || "";
  applyRoleView();
  const initialView = state.user.role === "admin" ? "admin" : state.user.role === "finance" ? "finance" : "submit";
  switchView(initialView, false);
  refresh();
}

async function request(url, options = {}) {
  const requestOptions = { ...options, headers: new Headers(options.headers || {}) };
  const method = String(requestOptions.method || "GET").toUpperCase();
  if (state.csrfToken && MUTATING_METHODS.has(method)) {
    requestOptions.headers.set("X-CSRF-Token", state.csrfToken);
  }
  const response = await fetch(url, requestOptions);
  const raw = await response.text();
  let data = {};
  try { data = raw ? JSON.parse(raw) : {}; } catch { data = { error: raw || "请求失败" }; }
  if (response.status === 401 && !url.startsWith("/api/login") && !url.startsWith("/api/setup")) {
    window.location.replace(data.setup_required ? "/setup.html" : "/login.html");
  }
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function statusClass(status) {
  return ["registered", "noncompliant", "review", "finance_pending", "processing", "pending"].includes(status) ? status : "review";
}

function formatTime(value) { return value ? value.replace("T", " ").slice(5, 16) : "—"; }
function confidence(value) { return value ? Math.round(value * 100) + "%" : "—"; }
function money(value, fallback = "待确认") { return value == null ? fallback : "¥ " + Number(value).toFixed(2); }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function originalPanel(item) {
  if (!item.original_available) {
    return "<div class=\"original-card\"><div class=\"original-heading\"><strong>发票原件</strong><span>当前任务没有可预览的上传文件</span></div></div>";
  }
  const name = escapeHtml(item.original_name || "发票原件");
  const url = escapeHtml(item.original_url);
  const isImage = /\.(jpe?g|png|gif|webp|bmp)$/i.test(item.original_name || "");
  const preview = isImage
    ? "<a class=\"original-preview\" href=\"" + url + "\" target=\"_blank\" rel=\"noopener\"><img src=\"" + url + "\" alt=\"" + name + "\"></a>"
    : "<div class=\"original-preview original-file-preview\">该文件不是图片，请点击“查看原件”打开</div>";
  return "<div class=\"original-card\"><div class=\"original-heading\"><div><strong>发票原件</strong><span>服务器受控归档</span></div><span class=\"original-name\">" + name + "</span></div>" + preview + "<div class=\"original-actions\"><a class=\"button ghost\" href=\"" + url + "\" target=\"_blank\" rel=\"noopener\">查看原件</a><a class=\"button ghost\" href=\"" + url + "\" download=\"" + name + "\">下载原件</a></div></div>";
}

async function loadDashboard() {
  if (!state.user || state.user.role !== "finance") return;
  const data = await request("/api/dashboard");
  $("#processed-count").textContent = data.processed_today.toLocaleString("zh-CN");
  $("#registered-count").textContent = data.registered.toLocaleString("zh-CN");
  $("#noncompliant-count").textContent = data.noncompliant.toLocaleString("zh-CN");
  $("#review-count").textContent = data.review.toLocaleString("zh-CN");
  $("#nav-review").textContent = data.review;
  $("#progress-percent").textContent = data.progress + "%";
  $("#progress-bar").style.width = data.progress + "%";
  $("#progress-text").textContent = "已处理 " + data.processed_today.toLocaleString("zh-CN") + " / 10,000 张";
  $("#processing-text").textContent = data.processing + " 张处理中";
  $("#engine-mode").textContent = data.ocr_mode;
}

function renderInvoices(items, tableId, emptyId) {
  const tbody = $(tableId);
  tbody.innerHTML = "";
  $(emptyId).hidden = items.length > 0;
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML = "<td><div class=\"file-cell\"><span class=\"file-icon\">DOC</span><span>" + escapeHtml(item.file_name) + "</span></div></td><td>" + escapeHtml(item.invoice_no || "待识别") + "</td><td class=\"confidence\">" + confidence(item.confidence) + "</td><td><span class=\"status " + statusClass(item.status) + "\">" + escapeHtml(item.status_label) + "</span></td><td>" + formatTime(item.updated_at) + "</td><td><button class=\"row-action\" aria-label=\"查看详情\">›</button></td>";
    row.querySelector(".row-action").addEventListener("click", () => openDetail(item.id));
    tbody.appendChild(row);
  });
}

async function loadCurrentList() {
  if (!state.user || !["tasks", "review"].includes(state.view)) return;
  const isReviewPage = state.view === "review";
  const params = new URLSearchParams();
  if (isReviewPage) params.set("status", "review");
  else if (state.filter !== "all") params.set("status", state.filter);
  if (state.search.trim()) params.set("search", state.search.trim());
  const query = params.toString() ? "?" + params.toString() : "";
  const data = await request("/api/invoices" + query);
  if (isReviewPage) {
    $("#review-page-count").textContent = data.items.length;
    renderInvoices(data.items, "#review-table", "#review-empty-state");
  } else {
    const titles = { all: "全部任务", registered: "已登记任务", noncompliant: "不合规任务", review: "待财务处理任务", finance_pending: "待财务确认任务" };
    $("#tasks-list-title").textContent = titles[state.filter] || "筛选任务";
    renderInvoices(data.items, "#tasks-table", "#tasks-empty-state");
  }
}

function applyRoleView() {
  const role = state.user ? state.user.role : "";
  const isEmployee = role === "employee";
  const isFinance = role === "finance";
  const isAdmin = role === "admin";
  $("#nav-finance").hidden = !isFinance;
  $("#nav-submit").hidden = !isEmployee;
  $("#nav-tasks").hidden = !isEmployee;
  $("#nav-review-item").hidden = true;
  $("#admin-nav").hidden = !isAdmin;
  $("#settings-nav").hidden = false;
  $("#export-btn").hidden = !isFinance;
  $("#refresh-btn").hidden = !isFinance;
  $("#sidebar-bottom").hidden = isEmployee;
  $("#current-user").textContent = state.user ? `${state.user.display_name} · ${state.user.role_label}` : "";
}

function allowedView(view) {
  const role = state.user && state.user.role;
  const allowed = {
    employee: ["submit", "tasks", "settings"],
    finance: ["finance", "tasks", "review", "settings"],
    admin: ["admin", "settings"],
  };
  return allowed[role] && allowed[role].includes(view) ? view : role === "admin" ? "admin" : role === "finance" ? "finance" : "submit";
}

function switchView(requestedView, shouldRefresh = true) {
  const view = allowedView(requestedView);
  const previousView = state.view;
  state.view = view;
  const targetIds = {
    finance: "finance-view",
    submit: "submit-view",
    tasks: "tasks-view",
    review: "review-view",
    admin: "admin-view",
    settings: "settings-view",
  };
  const targetId = targetIds[view];
  document.querySelectorAll(".view, .subpage").forEach((section) => { section.hidden = section.id !== targetId; });
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  if (view === "tasks") {
    if (previousView !== "tasks") state.filter = "all";
    const isEmployee = state.user && state.user.role === "employee";
    $("#tasks-page-title").textContent = isEmployee ? "我的发票" : "全部发票";
    $("#tasks-page-description").textContent = isEmployee ? "查看本人提交的发票任务及处理结果" : "查看全量发票任务及处理结果";
    $("#task-search").value = state.search;
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.filter === state.filter));
  }
  if (shouldRefresh) refresh();
}

async function openDetail(id) {
  try {
    const item = await request("/api/invoices/" + encodeURIComponent(id));
    $("#modal-title").textContent = item.file_name;
    const fields = item.fields || {};
    const values = [["任务编号", item.id], ["发票号码", item.invoice_no || "待识别"], ["开票日期", item.invoice_date || "待识别"], ["销售方", item.seller || "待识别"], ["购买方", item.buyer || "待识别"], ["价税合计", money(item.total, "待识别")], ["建议报销金额", money(fields.suggested_reimbursable_amount)], ["可登记金额", money(fields.reimbursable_amount, "需复核")], ["税前扣除参考", money(fields.tax_deductible_amount, "不适用")], ["识别置信度", confidence(item.confidence)], ["处理状态", item.status_label]];
    $("#modal-body").innerHTML = "<div class=\"detail-grid\">" + values.map(([label, value]) => "<div><span>" + label + "</span><strong>" + escapeHtml(value) + "</strong></div>").join("") + "</div>" + originalPanel(item) + "<div class=\"reason-box\">" + escapeHtml(item.reason || "暂无说明") + "</div>";
    const actions = $("#modal-actions");
    actions.innerHTML = "";
    if (item.status !== "registered") {
      actions.insertAdjacentHTML("beforeend", "<button class=\"button ghost\" data-delete>删除记录</button>");
      actions.querySelector("[data-delete]").addEventListener("click", () => deleteInvoice(item.id));
    }
    if (["review", "finance_pending"].includes(item.status) && state.user.role === "finance") {
      const passLabel = item.status === "finance_pending" ? "确认登记" : "复核通过并登记";
      actions.insertAdjacentHTML("beforeend", "<button class=\"button ghost\" data-review=\"reject\">确认不合规</button><button class=\"button primary\" data-review=\"pass\">" + passLabel + "</button>");
      actions.querySelectorAll("[data-review]").forEach((button) => button.addEventListener("click", () => review(item.id, button.dataset.review)));
    }
    $("#modal").hidden = false;
  } catch (error) { showToast(error.message); }
}

async function review(id, action) {
  try {
    await request("/api/review/" + encodeURIComponent(id), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });
    closeModal(); await refresh(); showToast(action === "pass" ? "已确认登记" : "已确认不合规");
  } catch (error) { showToast(error.message); }
}

async function deleteInvoice(id) {
  if (!window.confirm("删除后将同时移除归档原件。确定删除这条未登记发票记录吗？")) return;
  try {
    const result = await request("/api/invoices/" + encodeURIComponent(id), { method: "DELETE" });
    closeModal(); await refresh(); showToast(result.message || "发票任务已删除");
  } catch (error) { showToast(error.message); }
}

function closeModal() { $("#modal").hidden = true; }

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("读取文件失败：" + file.name));
    reader.readAsDataURL(file);
  });
}

async function uploadSingleFile(file) {
  if (file.size > 25 * 1024 * 1024) throw new Error(file.name + " 超过 25MB");
  const content = await readFileAsBase64(file);
  return request("/api/upload", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ file_name: file.name, content_base64: content }) });
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const allowed = /\.(jpe?g|png|pdf)$/i;
  const invalid = files.filter((file) => !allowed.test(file.name));
  if (invalid.length) showToast("已跳过不支持的文件：" + invalid.map((file) => file.name).join("、"));
  const validFiles = files.filter((file) => allowed.test(file.name));
  if (!validFiles.length) return;
  showToast("已接收 " + validFiles.length + " 个文件，Agent 正在处理…");
  let nextIndex = 0; let completed = 0; const failed = [];
  const worker = async () => {
    while (nextIndex < validFiles.length) {
      const file = validFiles[nextIndex++];
      try { await uploadSingleFile(file); }
      catch (error) { failed.push(error.message || (file.name + " 上传失败")); }
      finally { completed += 1; showToast("正在处理发票：" + completed + " / " + validFiles.length); }
    }
  };
  await Promise.all(Array.from({ length: Math.min(4, validFiles.length) }, () => worker()));
  await refresh();
  showToast(failed.length ? "有 " + failed.length + " 个文件上传失败：" + failed.join("、") : "批量上传完成，可在我的发票中查看结果");
}

async function loadUsers() {
  if (!state.user || state.user.role !== "admin") return;
  const data = await request("/api/users");
  const tbody = $("#users-table");
  tbody.innerHTML = "";
  $("#users-empty-state").hidden = data.items.length > 0;
  data.items.forEach((user) => {
    const row = document.createElement("tr");
    const activeClass = user.active ? "registered" : "inactive";
    const activeLabel = user.active ? "启用" : "停用";
    const toggleLabel = user.active ? "停用" : "启用";
    row.innerHTML = "<td>" + escapeHtml(user.username) + "</td><td>" + escapeHtml(user.display_name) + "</td><td>" + escapeHtml(user.department || "—") + "</td><td>" + escapeHtml(user.role_label) + "</td><td><span class=\"status " + activeClass + "\">" + activeLabel + "</span></td><td><button class=\"button ghost user-toggle\">" + toggleLabel + "</button> <button class=\"button ghost user-reset\">重置密码</button></td>";
    row.querySelector(".user-toggle").addEventListener("click", async () => {
      try { await request("/api/users/" + encodeURIComponent(user.id) + "/toggle", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ active: !user.active }) }); await loadUsers(); showToast(user.active ? "账号已停用" : "账号已启用"); }
      catch (error) { showToast(error.message); }
    });
    row.querySelector(".user-reset").addEventListener("click", async () => {
      const password = window.prompt("请输入新的初始密码（至少 8 位）：");
      if (!password) return;
      try { await request("/api/users/" + encodeURIComponent(user.id) + "/reset-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) }); showToast("密码已重置"); }
      catch (error) { showToast(error.message); }
    });
    tbody.appendChild(row);
  });
}

async function changePassword(event) {
  event.preventDefault();
  const currentPassword = $("#current-password").value;
  const newPassword = $("#new-password").value;
  const confirmPassword = $("#confirm-password").value;
  if (newPassword !== confirmPassword) {
    showToast("两次输入的新密码不一致");
    return;
  }
  try {
    await request("/api/account/change-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
    window.location.replace("/login.html");
  } catch (error) { showToast(error.message); }
}

async function refresh() {
  try {
    if (!state.user) return;
    if (state.user.role === "admin") {
      if (state.view === "admin") await loadUsers();
      return;
    }
    if (state.user.role === "finance") {
      if (state.view === "finance") await loadDashboard();
      if (["tasks", "review"].includes(state.view)) await loadCurrentList();
      return;
    }
    if (state.user.role === "employee" && state.view === "tasks") {
      await loadCurrentList();
    }
  } catch (error) { showToast(error.message); }
}

async function loadSession() {
  try {
    const data = await request("/api/me");
    if (data.authenticated) {
      setSession({ user: data.user, csrf_token: data.csrf_token || "" });
    } else {
      window.location.replace(data.setup_required ? "/setup.html" : "/login.html");
    }
  } catch (error) {
    window.location.replace("/login.html");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("#logout-btn").addEventListener("click", async () => {
    try { await request("/api/logout", { method: "POST" }); window.location.replace("/login.html"); }
    catch (error) { showToast(error.message); }
  });
  $("#refresh-btn").addEventListener("click", refresh);
  const uploadZone = $("#upload-zone");
  const fileInput = $("#file-input");
  fileInput.addEventListener("change", (event) => { uploadFiles(event.target.files); event.target.value = ""; });
  ["dragenter", "dragover"].forEach((eventName) => uploadZone.addEventListener(eventName, (event) => { event.preventDefault(); uploadZone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((eventName) => uploadZone.addEventListener(eventName, (event) => { event.preventDefault(); uploadZone.classList.remove("dragging"); }));
  uploadZone.addEventListener("drop", (event) => uploadFiles(event.dataTransfer.files));
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal").addEventListener("click", (event) => { if (event.target.id === "modal") closeModal(); });
  const taskSearch = $("#task-search");
  const runTaskSearch = () => {
    state.search = taskSearch.value.trim();
    if (state.view !== "tasks") switchView("tasks");
    else loadCurrentList().catch((error) => showToast(error.message));
  };
  $("#task-search-btn").addEventListener("click", runTaskSearch);
  taskSearch.addEventListener("keydown", (event) => { if (event.key === "Enter") runTaskSearch(); });
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    state.filter = tab.dataset.filter;
    if (state.view !== "tasks") switchView("tasks");
    else loadCurrentList().catch((error) => showToast(error.message));
  }));
  document.querySelectorAll(".nav-item, [data-open-view]").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view || item.dataset.openView)));
  $("#user-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await request("/api/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: $("#user-username").value, display_name: $("#user-display-name").value, department: $("#user-department").value, role: $("#user-role").value, password: $("#user-password").value }) });
      event.target.reset(); await loadUsers(); showToast("账号创建成功");
    } catch (error) { showToast(error.message); }
  });
  $("#password-form").addEventListener("submit", changePassword);
  loadSession();
});
