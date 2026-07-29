'use strict';
/* Mike Dashboard - Orchestration: Navigation, Event Listeners, and Init */

/* ===== Navigation ===== */
function switchPage(page) {
  state.page = page;
  var pages = ['page-chat', 'page-lousa', 'page-status'];
  for (var i = 0; i < pages.length; i++) {
    var el = document.getElementById(pages[i]);
    if (el) el.hidden = (pages[i] !== 'page-' + page);
  }
  var btns = document.querySelectorAll('.nav-btn[data-page]');
  for (var i = 0; i < btns.length; i++) {
    btns[i].classList.toggle('active', btns[i].dataset.page === page);
  }
  if (page === 'status') refreshStatusPage();
}

/* ===== Password Change ===== */
function openPwModal() {
  $("pw-old").value = ""; $("pw-new").value = ""; $("pw-confirm").value = "";
  $("pw-error").textContent = ""; $("pw-ok").textContent = "";
  $('pw-modal').classList.add('open');
  $('pw-old').focus();
}
function closePwModal() { $('pw-modal').classList.remove('open'); }

async function handlePwChange(e) {
  e.preventDefault();
  $("pw-error").textContent = ""; $("pw-ok").textContent = "";
  var oldPw = $("pw-old").value, newPw = $("pw-new").value, confirm = $("pw-confirm").value;
  if (!oldPw) { $("pw-error").textContent = "Digite a senha atual."; return; }
  if (newPw.length < 4) { $("pw-error").textContent = "Nova senha deve ter pelo menos 4 caracteres."; return; }
  if (newPw !== confirm) { $("pw-error").textContent = "Senhas nao conferem."; return; }
  try {
    await api("/v1/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
    });
    $("pw-ok").textContent = "Senha alterada com sucesso!";
    setTimeout(closePwModal, 1500);
  } catch (err) { $("pw-error").textContent = err.message; }
}

/* ===== Mobile Sidebar ===== */
function openSidebar() {
  $("sidebar").classList.add("open");
  $("sidebar-overlay").classList.add("open");
  document.body.style.overflow = "hidden";
}
function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("sidebar-overlay").classList.remove("open");
  document.body.style.overflow = "";
}

/* ===== Event Listeners ===== */
$("menu-btn").addEventListener("click", openSidebar);
$("sidebar-overlay").addEventListener("click", closeSidebar);

/* Mobile topbar visibility */
(function() {
  var mq = window.matchMedia("(max-width: 700px)");
  function applyMobile(m) { $("mobile-topbar").style.display = m.matches ? "flex" : "none"; }
  applyMobile(mq);
  mq.addEventListener("change", applyMobile);
})();

/* PWA install */
window.addEventListener("beforeinstallprompt", function(event) {
  event.preventDefault();
  state.installPrompt = event;
  refreshInstallCard();
});

window.addEventListener("appinstalled", function() {
  state.installPrompt = null;
  markInstallCardInstalled();
  refreshInstallCard();
});

if (window.matchMedia) {
  var standaloneQuery = window.matchMedia("(display-mode: standalone)");
  if (standaloneQuery && standaloneQuery.addEventListener) {
    standaloneQuery.addEventListener("change", refreshInstallCard);
  }
}

/* Auth / Session events */
$("login-form").addEventListener("submit", handleLogin);
$("logout-btn").addEventListener("click", handleLogout);
$("chpw-btn").addEventListener("click", openPwModal);
$("pw-cancel").addEventListener("click", closePwModal);
$("pw-form").addEventListener("submit", handlePwChange);
$("pw-modal").addEventListener("click", function(e) { if (e.target === $("pw-modal")) closePwModal(); });
$("new-chat-btn").addEventListener("click", function() {
  if (state.profile && !state.streaming) startSession(state.profile, { forceNew: true });
});

/* Navigation */
var navBtns = document.querySelectorAll('.nav-btn[data-page]');
for (var ni = 0; ni < navBtns.length; ni++) {
  (function(btn) { btn.addEventListener('click', function() { switchPage(btn.dataset.page); }); })(navBtns[ni]);
}
$('tools-drawer-btn') && $('tools-drawer-btn').addEventListener('click', function() { switchPage('status'); });
var _drawerOverlay = $('tools-drawer-overlay');
if (_drawerOverlay) _drawerOverlay.addEventListener('click', function() {});
document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && state.page === 'status') switchPage('chat'); });

/* Status page events */
var _rbtn = $('refresh-status-btn');
if (_rbtn) _rbtn.addEventListener('click', refreshStatusPage);

var _seForm = $('status-email-form');
if (_seForm) _seForm.addEventListener('submit', async function(e) {
  e.preventDefault();
  var to = $('status-email-to').value.trim();
  var subject = $('status-email-subject').value.trim();
  var body = $('status-email-body').value.trim();
  var res = $('status-email-result');
  if (!to || !subject || !body) { if (res) res.textContent = '⚠️ Preencha todos os campos.'; return; }
  if (res) res.textContent = '⏳ Enviando...';
  try {
    var r = await api('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'Envie um email para ' + to + ' com assunto "' + subject + '" e corpo: ' + body }],
        stream: false
      })
    });
    if (res) res.textContent = '✅ Comando enviado ao Mike.';
  } catch(err) {
    if (res) res.textContent = '❌ Erro: ' + err.message;
  }
});

/* Auto-refresh status every 30s when on status page */
setInterval(function() { if (state.page === 'status') refreshStatusPage(); }, 30000);

/* Task badge poll: refresh pending task count every 60s for nav badge */
function refreshTaskBadge() {
  var badge = document.getElementById('task-badge');
  if (!badge) return;
  var pending = 0;
  // Read from localStorage (matches the active Lousa task board in tasks.js)
  try {
    var raw = localStorage.getItem('mike_lousa_tasks');
    if (raw) {
      var tasks = JSON.parse(raw);
      for (var i = 0; i < tasks.length; i++) {
        if (!tasks[i].done) pending++;
      }
    }
  } catch(e) { /* ignore corrupt storage */ }
  badge.textContent = pending;
  badge.style.display = pending > 0 ? '' : 'none';
}
setInterval(refreshTaskBadge, 60000);
/* Fire once on load after a short delay (wait for session to be ready) */
setTimeout(refreshTaskBadge, 5000);

/* Chat / Composer events */
$("send-btn").addEventListener("click", function() { sendMessage(); });
$("stop-btn").addEventListener("click", stopStream);
$("attach-btn").addEventListener("click", function() { $("file-input").click(); });
$("camera-btn").addEventListener("click", function() { $("camera-input").click(); });
$("file-input").addEventListener("change", handleImageSelect);
$("camera-input").addEventListener("change", handleImageSelect);
$("mic-btn").addEventListener("click", toggleMic);
$("speak-btn").addEventListener("click", speakLastResponse);

$("chat-input").addEventListener("input", autoResize);
$("chat-input").addEventListener("keydown", function(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

$("img-preview").addEventListener("click", function(e) {
  var btn = e.target.closest(".rm-img");
  if (btn) {
    state.images.splice(Number(btn.dataset.idx), 1);
    renderImgPreview();
    if (!state.images.length) clearComposerNote();
  }
});

/* Session sidebar */
$("session-list").addEventListener("click", function(e) {
  var btn = e.target.closest(".session-btn");
  if (btn && state.profile && !state.streaming) {
    startSession(state.profile, { sid: btn.dataset.sid });
  }
});

/* Suggestions */
$("suggestions").addEventListener("click", function(e) {
  var chip = e.target.closest(".chip");
  if (!chip) return;
  var prompt = chip.dataset.prompt;
  if (prompt.endsWith(" ")) {
    $("chat-input").value = prompt;
    $("chat-input").focus();
  } else {
    sendMessage(prompt);
  }
});

/* Privacy toggle */
$("privacy-toggle").addEventListener("change", function(e) {
  if (!state.profile || state.streaming) {
    updatePrivacyUi();
    return;
  }
  setPrivateMode(!!e.target.checked);
});

/* Stats / Skills / Tools */
var _refreshStatsBtn = $("refresh-stats");
if (_refreshStatsBtn) _refreshStatsBtn.addEventListener("click", refreshStatusPage);
var _refreshSkillsBtn = $("refresh-skills");
if (_refreshSkillsBtn) _refreshSkillsBtn.addEventListener("click", refreshSkills);
var _runMatchBtn = $("run-match");
if (_runMatchBtn) _runMatchBtn.addEventListener("click", runMatch);
var _matchInput = $("match-task-input");
if (_matchInput) _matchInput.addEventListener("keydown", function(e) { if (e.key === "Enter") runMatch(); });
var _toolSelect = $("tool-select");
if (_toolSelect) _toolSelect.addEventListener("change", handleToolSelect);
var _runToolBtn = $("run-tool");
if (_runToolBtn) _runToolBtn.addEventListener("click", runTool);

/* Email form */
var _emailForm = $("email-form");
if (_emailForm) _emailForm.addEventListener("submit", handleEmail);

/* Install card */
var installAppBtn = $("install-app-btn");
if (installAppBtn) installAppBtn.addEventListener("click", handleInstallApp);
var downloadShortcutBtn = $("download-shortcut-btn");
if (downloadShortcutBtn) downloadShortcutBtn.addEventListener("click", handleShortcutDownload);

/* File browser */
var _fileTree = $("file-tree");
if (_fileTree) {
  _fileTree.addEventListener("click", function(e) {
    var entry = e.target.closest(".fentry");
    if (!entry) return;
    if (entry.dataset.dir === "true") loadFiles(entry.dataset.path);
    else viewFile(entry.dataset.path);
  });
}

/* ===== Init ===== */
$("login-profile").value = "marco";
autoResize();
refreshInstallCard();

/* --- Server connection screen (Capacitor / remote mode) --- */
function needsServerConnect() {
  return !API || API === "null" || API === "undefined";
}

function showConnectScreen() {
  $("connect-screen").hidden = false;
  $("login-screen").hidden = true;
  $("login-screen").style.display = "none";
  $("app-screen").hidden = true;
  var urlInput = $("connect-url");
  urlInput.value = localStorage.getItem("mike_server_url") || "https://mike.supereziorealtime.com";
  urlInput.focus();
}

$("connect-form").addEventListener("submit", async function(e) {
  e.preventDefault();
  var url = $("connect-url").value.trim().replace(/\/+$/, "");
  $("connect-error").textContent = "";
  if (!url || !/^https?:\/\/.+/.test(url)) {
    $("connect-error").textContent = "Endereco invalido. Use https://...";
    return;
  }
  $("connect-error").textContent = "Conectando...";
  try {
    var resp = await fetch(url + "/health", { mode: "cors" });
    if (!resp.ok) throw new Error("Servidor respondeu " + resp.status);
  } catch (err) {
    $("connect-error").textContent = "Nao consegui conectar: " + err.message;
    return;
  }
  localStorage.setItem("mike_server_url", url);
  // Reload the page so API constant picks up the new URL
  location.reload();
});

/* Initialize Lousa / Task Board */
lousaInitEvents();

/* Bootstrap app */
(async function() {
  if (needsServerConnect()) {
    showConnectScreen();
    return;
  }
  // Never leave the page blank while session restoration is in progress.
  // If a valid cookie exists, startSession replaces this screen immediately.
  showLoginScreen();
  var restored = await tryRestore();
  if (!restored) {
    // When profile auth is enabled, show login screen with password
    // so the server issues a proper session cookie.
    // Without the cookie, TRUST_LOCALHOST lets requests through but
    // profile_key is None and write tools are denied.
    if (state.bootstrap && state.bootstrap.profile_auth_enabled) {
      showLoginScreen();
    } else {
      startIdentifying();
    }
  }
})();
