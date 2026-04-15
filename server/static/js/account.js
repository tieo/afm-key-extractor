// Apple ID / account panel.

import { postJSON } from "./api.js";
import { state, refreshStatus } from "./state.js";

let pendingMethods = [];
let selectedMethod = 0;
let awaiting2fa = false;

export function renderAccount(error) {
  const body = document.getElementById("account-body");
  if (state.account.configured) {
    awaiting2fa = false;
    body.innerHTML = `
      <div class="section">
        <h3>Connected</h3>
        <p><span class="dot green"></span> Signed in. Tracking ${state.account.airtags} key${state.account.airtags !== 1 ? "s" : ""}.</p>
        <button class="btn small" data-action="relogin">Re-login</button>
      </div>`;
    body.querySelector('[data-action="relogin"]').addEventListener("click", () => { awaiting2fa = false; showLoginForm(); });
    return;
  }
  if (awaiting2fa) return;
  showLoginForm(error);
}

export function showLoginForm(error) {
  const body = document.getElementById("account-body");
  body.innerHTML = `
    <div class="section">
      <h3>Sign in with Apple ID</h3>
      <p>Needed to query the Find My network. Stored encrypted on this server only.</p>
      ${error ? `<div class="error">${error}</div>` : ""}
      <input type="email" id="login-email" placeholder="Apple ID (email)" autocomplete="email">
      <input type="password" id="login-password" placeholder="Password" autocomplete="current-password">
      <button class="btn primary" data-action="login">Sign In</button>
    </div>`;
  body.querySelector('[data-action="login"]').addEventListener("click", doLogin);
}

async function doLogin() {
  const email = document.getElementById("login-email").value;
  const password = document.getElementById("login-password").value;
  if (!email || !password) return;
  document.getElementById("account-body").innerHTML =
    `<div class="section"><h3>Signing in…</h3><p>Authenticating with Apple.</p></div>`;
  try {
    const { data } = await postJSON("/api/account/login", { email, password });
    if (data.status === "2fa_required") { pendingMethods = data.methods; selectedMethod = 0; awaiting2fa = true; show2faForm(); }
    else if (data.status === "logged_in") { awaiting2fa = false; await refreshStatus(); renderAccount(); }
    else { awaiting2fa = false; showLoginForm(data.error || "Login failed"); }
  } catch (e) { showLoginForm("Network error: " + e.message); }
}

function show2faForm(error) {
  const method = pendingMethods[selectedMethod];
  const hint = method.type === "sms"
    ? "Enter the code sent to " + method.phone
    : "Enter the code shown on your trusted device";
  let methodsHtml = "";
  if (pendingMethods.length > 1) {
    methodsHtml = '<div style="margin:8px 0">' + pendingMethods.map((m, i) => {
      const label = m.type === "sms" ? `SMS: ${m.phone}` : "Trusted Device";
      return `<label class="check"><input type="radio" name="2fa-method" data-index="${i}" ${i === selectedMethod ? "checked" : ""}> ${label}</label>`;
    }).join("") + "</div>";
  }
  const body = document.getElementById("account-body");
  body.innerHTML = `
    <div class="section">
      <h3>Two-Factor Authentication</h3>
      <p>${hint}</p>
      ${methodsHtml}
      ${error ? `<div class="error">${error}</div>` : ""}
      <input type="text" id="2fa-code" placeholder="6-digit code" maxlength="6" inputmode="numeric" autocomplete="one-time-code">
      <button class="btn primary" data-action="submit-2fa">Verify</button>
    </div>`;
  body.querySelectorAll('input[name="2fa-method"]').forEach((r) => {
    r.addEventListener("click", () => { selectedMethod = +r.dataset.index; show2faForm(); });
  });
  body.querySelector('[data-action="submit-2fa"]').addEventListener("click", submit2fa);
  document.getElementById("2fa-code").focus();
}

async function submit2fa() {
  const code = document.getElementById("2fa-code").value;
  if (!code) return;
  try {
    const { data } = await postJSON("/api/account/2fa", { code, method: selectedMethod });
    if (data.status === "logged_in") { awaiting2fa = false; await refreshStatus(); renderAccount(); }
    else show2faForm(data.error || "2FA failed");
  } catch (e) { show2faForm("Network error: " + e.message); }
}
