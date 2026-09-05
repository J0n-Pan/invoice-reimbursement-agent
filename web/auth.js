const authPage = document.body.dataset.page;
const authError = document.querySelector("#auth-error");

function showAuthError(message) {
  if (authError) authError.textContent = message || "";
}

async function authRequest(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const raw = await response.text();
  let data = {};
  try { data = raw ? JSON.parse(raw) : {}; } catch { data = { error: raw || "请求失败" }; }
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

async function checkAuthState() {
  const response = await fetch("/api/me", { cache: "no-store" });
  const data = await response.json();
  if (data.authenticated) {
    window.location.replace("/workspace.html");
    return true;
  }
  if (authPage === "login" && data.setup_required) {
    window.location.replace("/setup.html");
    return true;
  }
  if (authPage === "setup" && !data.setup_required) {
    window.location.replace("/login.html");
    return true;
  }
  return false;
}

document.addEventListener("DOMContentLoaded", async () => {
  try {
    if (await checkAuthState()) return;
  } catch {
    showAuthError("无法连接服务，请确认 Agent 已启动");
    return;
  }

  const form = document.querySelector(authPage === "setup" ? "#setup-form" : "#login-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    showAuthError("");
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      const data = authPage === "setup"
        ? await authRequest("/api/setup", {
            username: document.querySelector("#setup-username").value,
            display_name: document.querySelector("#setup-display-name").value,
            password: document.querySelector("#setup-password").value,
          })
        : await authRequest("/api/login", {
            username: document.querySelector("#login-username").value,
            password: document.querySelector("#login-password").value,
          });
      if (data.user) window.location.replace("/workspace.html");
    } catch (error) {
      showAuthError(error.message || "操作失败");
    } finally {
      if (button) button.disabled = false;
    }
  });
});
