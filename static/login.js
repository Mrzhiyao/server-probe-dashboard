const form = document.querySelector("#loginForm");
const button = document.querySelector("#loginButton");
const errorBox = document.querySelector("#loginError");

function nextPath() {
  const params = new URLSearchParams(window.location.search);
  const next = params.get("next") || "/";
  return next.startsWith("/") ? next : "/";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.textContent = "";
  button.disabled = true;
  button.textContent = "登录中";
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: form.username.value.trim(),
        password: form.password.value,
      }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    const session = await fetch("/api/auth/me", {
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
    });
    const sessionPayload = await session.json().catch(() => ({}));
    if (!session.ok || !sessionPayload.authenticated) {
      throw new Error("登录状态未能保存，请刷新页面后重试");
    }
    window.location.replace(nextPath());
  } catch (error) {
    errorBox.textContent = error.name === "AbortError" ? "登录请求超时，请检查网络后重试" : error.message || "登录失败";
  } finally {
    window.clearTimeout(timeout);
    button.disabled = false;
    button.textContent = "登录";
  }
});
