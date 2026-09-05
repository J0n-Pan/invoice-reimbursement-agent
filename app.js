const state = { filter: "all" };
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

async function loadInvoices() {
  const query = state.filter === "all" ? "" : "?status=" + encodeURIComponent(state.filter);
  const data = await request("/api/invoices" + query);
  const tbody = $("#invoice-table");
  tbody.innerHTML = "";
  $("#empty-state").hidden = data.items.length > 0;
  data.items.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML = "<td><div class=\"file-cell\"><span class=\"file-icon\">DOC</span><span>" + escapeHtml(item.file_name) + "</span></div></td><td>" + escapeHtml(item.invoice_no || "待识别") + "</td><td class=\"confidence\">" + confidence(item.confidence) + "</td><td><span class=\"status " + statusClass(item.status) + "\">" + escapeHtml(item.status_label) + "</span></td><td>" + formatTime(item.updated_at) + "</td><td><button class=\"row-action\" aria-label=\"查看详情\">›</button></td>";
    row.querySelector(".row-action").addEventListener("click", () => openDetail(item.id));
    tbody.appendChild(row);
  });
}

async function openDetail(id) {
  try {
    const item = await request("/api/invoices/" + encodeURIComponent(id));
    $("#modal-title").textContent = item.file_name;
    const fields = item.fields || {};
    const values = [["任务编号", item.id], ["发票号码", item.invoice_no || "待识别"], ["开票日期", item.invoice_date || "待识别"], ["销售方", item.seller || "待识别"], ["购买方", item.buyer || "待识别"], ["价税合计", money(item.total, "待识别")], ["建议报销金额", money(fields.suggested_reimbursable_amount)], ["可登记金额", money(fields.reimbursable_amount, "需复核")], ["税前扣除参考", money(fields.tax_deductible_amount, "不适用")], ["识别置信度", confidence(item.confidence)], ["处理状态", item.status_label]];
    $("#modal-body").innerHTML = "<div class=\"detail-grid\">" + values.map(([label, value]) => "<div><span>" + label + "</span><strong>" + escapeHtml(value) + "</strong></div>").join("") + "</div><div class=\"reason-box\">" + escapeHtml(item.reason || "暂无说明") + "</div>";
    const actions = $("#modal-actions");
    actions.innerHTML = "";
    if (item.status === "review") {
      actions.innerHTML = "<button class=\"button ghost\" data-review=\"reject\">确认不合规</button><button class=\"button primary\" data-review=\"pass\">复核通过</button>";
      actions.querySelectorAll("[data-review]").forEach((button) => button.addEventListener("click", () => review(item.id, button.dataset.review)));
    }
    $("#modal").hidden = false;
  } catch (error) { showToast(error.message); }
}

async function review(id, action) {
  try {
    await request("/api/review/" + encodeURIComponent(id), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });
    closeModal(); await refresh(); showToast(action === "pass" ? "已复核通过，进入登记队列" : "已确认不合规，完成文档录入");
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

async function uploadFile(file) {
  if (!file) return;
  if (file.size > 25 * 1024 * 1024) return showToast("单个文件不能超过 25MB");
  showToast("文件已接收，Agent 正在处理…");
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const content = String(reader.result).split(",")[1] || "";
      await request("/api/upload", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ file_name: file.name, content_base64: content }) });
      await refresh(); showToast("处理完成，可在任务流中查看结果");
    } catch (error) { showToast(error.message); }
  };
  reader.readAsDataURL(file);
}

async function refresh() {
  try { await Promise.all([loadDashboard(), loadInvoices()]); }
  catch (error) { showToast(error.message); }
}

document.addEventListener("DOMContentLoaded", () => {
  $("#refresh-btn").addEventListener("click", refresh);
  $("#demo-btn").addEventListener("click", runDemo);
  $("#file-input").addEventListener("change", (event) => uploadFile(event.target.files[0]));
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal").addEventListener("click", (event) => { if (event.target.id === "modal") closeModal(); });
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => { document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active")); tab.classList.add("active"); state.filter = tab.dataset.filter; loadInvoices(); }));
  document.querySelectorAll(".nav-item").forEach((nav) => nav.addEventListener("click", () => { document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active")); nav.classList.add("active"); state.filter = nav.dataset.view === "review" ? "review" : "all"; document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.filter === state.filter)); loadInvoices(); }));
  refresh();
});
