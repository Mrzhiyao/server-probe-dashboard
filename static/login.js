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
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
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
    window.location.href = nextPath();
  } catch (error) {
    errorBox.textContent = error.message || "登录失败";
  } finally {
    button.disabled = false;
    button.textContent = "登录";
  }
});
