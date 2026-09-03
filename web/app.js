const state = { view: "dashboard", filter: "all", search: "" };
const $ = (selector) => document.querySelector(selector);

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2600);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function statusClass(status) { return ["registered", "noncompliant", "review", "processing", "pending"].includes(status) ? status : "review"; }
function formatTime(value) { return value ? value.replace("T", " ").slice(5, 16) : "—"; }
function confidence(value) { return value ? Math.round(value * 100) + "%" : "—"; }
function money(value, fallback = "待确认") { return value == null ? fallback : "¥ " + Number(value).toFixed(2); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char])); }
function originalPanel(item) {
  if (!item.original_available) return "<div class=\"original-card\"><div class=\"original-heading\"><strong>发票原件</strong><span>当前任务没有可预览的上传文件</span></div></div>";
  const name = escapeHtml(item.original_name || "发票原件");
  const url = escapeHtml(item.original_url);
  const isImage = /\.(jpe?g|png|gif|webp|bmp)$/i.test(item.original_name || "");
  const preview = isImage ? "<a class=\"original-preview\" href=\"" + url + "\" target=\"_blank\" rel=\"noopener\"><img src=\"" + url + "\" alt=\"" + name + "\"></a>" : "<div class=\"original-preview original-file-preview\">该文件不是图片，请点击“查看原件”打开</div>";
  return "<div class=\"original-card\"><div class=\"original-heading\"><div><strong>发票原件</strong><span>统一归档：upload_invoice</span></div><span class=\"original-name\">" + name + "</span></div>" + preview + "<div class=\"original-actions\"><a class=\"button ghost\" href=\"" + url + "\" target=\"_blank\" rel=\"noopener\">查看原件</a><button class=\"button ghost\" data-open-folder=\"" + escapeHtml(item.id) + "\">在本地文件夹中打开</button></div></div>";
}

async function loadDashboard() {
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
  if (state.view === "dashboard") return;
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
    const titles = { all: "全部任务", registered: "已登记任务", noncompliant: "不合规任务", review: "待复核任务" };
    $("#tasks-list-title").textContent = titles[state.filter] || "筛选任务";
    renderInvoices(data.items, "#tasks-table", "#tasks-empty-state");
  }
}

function switchView(view) {
  state.view = view;
  const targetId = view === "dashboard" ? "dashboard-view" : view === "tasks" ? "tasks-view" : "review-view";
  document.querySelectorAll(".subpage, #dashboard-view").forEach((section) => { section.hidden = section.id !== targetId; });
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  if (view === "tasks") {
    state.filter = "all";
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.filter === "all"));
    const searchInput = $("#task-search");
    if (searchInput) searchInput.value = state.search;
  }
  loadCurrentList().catch((error) => showToast(error.message));
}

async function openDetail(id) {
  try {
    const item = await request("/api/invoices/" + encodeURIComponent(id));
    $("#modal-title").textContent = item.file_name;
    const fields = item.fields || {};
    const values = [["任务编号", item.id], ["发票号码", item.invoice_no || "待识别"], ["开票日期", item.invoice_date || "待识别"], ["销售方", item.seller || "待识别"], ["购买方", item.buyer || "待识别"], ["价税合计", money(item.total, "待识别")], ["建议报销金额", money(fields.suggested_reimbursable_amount)], ["可登记金额", money(fields.reimbursable_amount, "需复核")], ["税前扣除参考", money(fields.tax_deductible_amount, "不适用")], ["识别置信度", confidence(item.confidence)], ["处理状态", item.status_label]];
    $("#modal-body").innerHTML = "<div class=\"detail-grid\">" + values.map(([label, value]) => "<div><span>" + label + "</span><strong>" + escapeHtml(value) + "</strong></div>").join("") + "</div>" + originalPanel(item) + "<div class=\"reason-box\">" + escapeHtml(item.reason || "暂无说明") + "</div>";
    const folderButton = $("#modal-body [data-open-folder]");
    if (folderButton) folderButton.addEventListener("click", () => openFolder(item.id));
    const actions = $("#modal-actions");
    actions.innerHTML = "<button class=\"button ghost\" data-delete>删除记录</button>";
    actions.querySelector("[data-delete]").addEventListener("click", () => deleteInvoice(item.id));
    if (item.status === "review") {
      actions.insertAdjacentHTML("beforeend", "<button class=\"button ghost\" data-review=\"reject\">确认不合规</button><button class=\"button primary\" data-review=\"pass\">复核通过</button>");
      actions.querySelectorAll("[data-review]").forEach((button) => button.addEventListener("click", () => review(item.id, button.dataset.review)));
    }
    $("#modal").hidden = false;
  } catch (error) { showToast(error.message); }
}

async function openFolder(id) {
  try {
    const result = await request("/api/invoices/" + encodeURIComponent(id) + "/open-folder");
    showToast(result.message || "已打开统一归档文件夹");
  } catch (error) { showToast(error.message); }
}

async function review(id, action) {
  try {
    await request("/api/review/" + encodeURIComponent(id), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });
    closeModal(); await refresh(); showToast(action === "pass" ? "已复核通过，进入登记队列" : "已确认不合规，完成文档录入");
  } catch (error) { showToast(error.message); }
}

async function deleteInvoice(id) {
  if (!window.confirm("删除后将同时移除归档原件，且不可恢复。确定删除这条发票记录吗？")) return;
  try {
    const result = await request("/api/invoices/" + encodeURIComponent(id), { method: "DELETE" });
    closeModal();
    await refresh();
    showToast(result.message || "发票任务已删除");
  } catch (error) { showToast(error.message); }
}

function closeModal() { $("#modal").hidden = true; }

async function runDemo() {
  const button = $("#demo-btn");
  button.disabled = true; button.innerHTML = "正在运行示例…";
  try { await request("/api/demo", { method: "POST" }); await refresh(); showToast("示例流程已完成"); }
  catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.innerHTML = "运行示例流程 <span>→</span>"; }
}

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
  if (invalid.length) {
    showToast("已跳过不支持的文件：" + invalid.map((file) => file.name).join("、"));
  }
  const validFiles = files.filter((file) => allowed.test(file.name));
  if (!validFiles.length) return;

  showToast("已接收 " + validFiles.length + " 个文件，Agent 正在处理…");
  let nextIndex = 0;
  let completed = 0;
  const failed = [];
  const worker = async () => {
    while (nextIndex < validFiles.length) {
      const file = validFiles[nextIndex++];
      try {
        await uploadSingleFile(file);
      } catch (error) {
        failed.push(error.message || (file.name + " 上传失败"));
      } finally {
        completed += 1;
        showToast("正在处理发票：" + completed + " / " + validFiles.length);
      }
    }
  };
  const workers = Array.from({ length: Math.min(4, validFiles.length) }, () => worker());
  await Promise.all(workers);
  await refresh();
  if (failed.length) {
    showToast("有 " + failed.length + " 个文件上传失败：" + failed.join("、"));
  } else {
    showToast("批量上传完成，可在任务流中查看结果");
  }
}

async function refresh() {
  try { await Promise.all([loadDashboard(), loadCurrentList()]); }
  catch (error) { showToast(error.message); }
}

document.addEventListener("DOMContentLoaded", () => {
  $("#refresh-btn").addEventListener("click", refresh);
  $("#demo-btn").addEventListener("click", runDemo);
  const uploadZone = $("#upload-zone");
  const fileInput = $("#file-input");
  fileInput.addEventListener("change", (event) => {
    uploadFiles(event.target.files);
    event.target.value = "";
  });
  ["dragenter", "dragover"].forEach((eventName) => uploadZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    uploadZone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((eventName) => uploadZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    uploadZone.classList.remove("dragging");
  }));
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
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => { document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active")); tab.classList.add("active"); state.view = "tasks"; state.filter = tab.dataset.filter; loadCurrentList(); }));
  document.querySelectorAll(".nav-item").forEach((nav) => nav.addEventListener("click", () => switchView(nav.dataset.view)));
  refresh();
});
