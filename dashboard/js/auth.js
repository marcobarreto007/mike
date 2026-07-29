/* Mike Dashboard - Auth, Sessions, and Conversational Identification */

/* ===== Sessions ===== */
async function loadSessions() {
  if (!state.profile || isPrivateMode()) {
    state.sessions = [];
    renderSessions();
    return [];
  }
  try {
    var data = await api("/v1/chat/sessions?profile=" + encodeURIComponent(state.profile) + "&limit=30");
    state.sessions = data.sessions || [];
  } catch (err) {
    state.sessions = [];
  }
  renderSessions();
  return state.sessions;
}

async function loadHistory(sessionId) {
  if (!sessionId || isPrivateMode()) {
    state.transcript = [];
    renderChat();
    return [];
  }
  try {
    var data = await api(
      "/v1/chat/history?session_id=" + encodeURIComponent(sessionId) +
      "&profile=" + encodeURIComponent(state.profile || "")
    );
    state.transcript = (data.messages || []).map(function(msg) {
      return {
        role: msg.role === "assistant" ? "assistant" : "user",
        text: msg.text || "",
        at: msg.timestamp || null,
      };
    });
  } catch (err) {
    state.transcript = [];
  }
  renderChat();
  return state.transcript;
}

async function setPrivateMode(nextMode, opts) {
  opts = opts || {};
  var forcedPrivate = state.profile === "visitante";
  state.privateMode = forcedPrivate || !!nextMode;
  state.images = [];
  clearComposerNote();
  renderImgPreview();
  updatePrivacyUi();
  if (!opts.preserveTranscript) {
    state.sessionId = createSessionId(state.profile || "main");
    state.transcript = [];
  }
  $("user-session").textContent = state.sessionId ? state.sessionId.slice(0, 20) : "-";
  if (isPrivateMode()) {
    state.sessions = [];
    renderSessions();
    renderChat();
    return;
  }
  await loadSessions();
  renderChat();
}

/* ===== Conversational Identification ===== */
function startIdentifying() {
  state.identifying = true;
  state.identifyAttempts = 0;
  state.profile = null;
  state.sessionId = null;
  state.sessions = [];
  state.transcript = [];
  state.images = [];
  state.privateMode = false;
  renderImgPreview();
  clearComposerNote();
  updatePrivacyUi();
  $('login-screen').hidden = true;
  $('login-screen').style.display = 'none';
  $('app-screen').hidden = false;
  $('user-name').textContent = '?';
  $('user-session').textContent = '-';
  $('welcome-name').textContent = 'ai';
  /* renderSuggestions(); — removed */
  renderChat();
  switchPage('chat');
  // Hide suggestion chips during identification
  $('suggestions').hidden = true;
  setTimeout(function() {
    appendMsg('ai', 'Oi! Sou o Mike — o companheiro da família Barreto. 🐾\n\nPara eu lembrar de você, me diz seu nome ou apelido.');
    $('chat-input').focus();
  }, 300);
}

/* ===== Auth ===== */
function showWhoAreYou() {
  var sub = $("login-subtitle");
  if (sub) sub.textContent = "Ola! Quem e voce?";
  var pwLabel = $("login-password-label");
  if (pwLabel) pwLabel.hidden = true;
  var profileLabel = $("login-profile-label-text");
  if (profileLabel) profileLabel.textContent = "Eu sou...";
  var btn = $("login-btn");
  if (btn) btn.textContent = "Continuar";
}

function showLoginScreen() {
  state.identifying = false;
  state.profile = null;
  var sub = $("login-subtitle");
  if (sub) sub.textContent = "Yorkshire Operator Console";
  var pwLabel = $("login-password-label");
  if (pwLabel) pwLabel.hidden = false;
  var profileLabel = $("login-profile-label-text");
  if (profileLabel) profileLabel.textContent = "Perfil";
  var btn = $("login-btn");
  if (btn) btn.textContent = "Entrar";
  $("login-error").textContent = "";
  $("login-screen").hidden = false;
  $("login-screen").style.display = "";
  $("app-screen").hidden = true;
}

async function handleLogin(e) {
  e.preventDefault();
  var profile = $("login-profile").value;
  $("login-error").textContent = "";
  if (!profile) { $("login-error").textContent = "Selecione quem voce e."; return; }
  // Modo sem autenticacao: entra direto sem senha
  if (isProfileAuthDisabled()) {
    await startSession(profile);
    return;
  }
  var password = $("login-password").value.trim();
  if (!password) { $("login-error").textContent = "Digite a senha."; return; }
  try {
    await api("/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: profile, password: password }),
    });
    $("login-password").value = "";
    await refreshBootstrap();
    await startSession(profile);
  } catch (err) {
    $("login-error").textContent = err.message;
  }
}

async function handleLogout() {
  stopStream();
  stopProactiveAlerts();
  try { await api("/v1/auth/logout", { method: "POST" }); } catch (e) {}
  state.bootstrap = null;
  state.toolSummary = null;
  state.opsAccess = false;
  applyOpsAccess();
  // When profile auth is enabled, show login screen so user gets a proper
  // session cookie on next login (required for write tool permissions).
  try { state.bootstrap = await refreshBootstrap(); } catch (e) {}
  if (state.bootstrap && state.bootstrap.profile_auth_enabled) {
    showLoginScreen();
  } else {
    startIdentifying();
  }
}

async function tryRestore() {
  // --- Magic link auto-login (link com ?magic=TOKEN enviado pelo WhatsApp) ---
  var urlParams = new URLSearchParams(location.search);
  var magicToken = urlParams.get('magic') || urlParams.get('m');
  if (magicToken) {
    history.replaceState(null, '', location.pathname); // limpa o token da URL
    try {
      var magicData = await api('/v1/auth/magic/use', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: magicToken }),
      });
      if (magicData && magicData.profile && magicData.profile.profile) {
        await refreshBootstrap();
        await startSession(magicData.profile.profile);
        return true;
      }
    } catch (magicErr) {
      // Token invalido ou expirado — continua para login normal
      console.warn('Magic token invalido:', magicErr.message);
    }
  }

  try {
    var bootstrap = await refreshBootstrap();
    if (bootstrap && bootstrap.profile_auth_enabled === false) {
      showWhoAreYou();
    }
    if (bootstrap && bootstrap.authenticated && bootstrap.profile && PROFILES[bootstrap.profile.profile]) {
      await startSession(bootstrap.profile.profile);
      return true;
    }
  } catch (e) {}
  return false;
}

async function startSession(profile, opts) {
  opts = opts || {};
  state.profile = profile;
  state.privateMode = profile === "visitante" ? true : !!opts.privateMode;
  state.sessionId = opts.sid || createSessionId(profile);
  state.sessions = [];
  state.transcript = [];
  state.images = [];
  renderImgPreview();
  clearComposerNote();
  if (!state.bootstrap) {
    try { await refreshBootstrap(); } catch (e) {}
  } else {
    applyBootstrap(state.bootstrap);
  }

  $("login-screen").hidden = true;
  $("login-screen").style.display = "none";
  $("app-screen").hidden = false;
  $("user-name").textContent = PROFILES[profile].name;
  $("welcome-name").textContent = PROFILES[profile].name;

  /* renderSuggestions(); — removed */
  switchPage("chat");
  updatePrivacyUi();
  if (!isPrivateMode()) {
    await loadSessions();
    if (!opts.forceNew && !opts.sid && state.sessions.length) {
      state.sessionId = state.sessions[0].session_id;
    }
    if (!opts.forceNew) {
      await loadHistory(state.sessionId);
    } else {
      renderChat();
    }
  } else {
    renderSessions();
    renderChat();
  }
  $("user-session").textContent = state.sessionId ? state.sessionId.slice(0, 20) : "-";
  $("chat-input").focus();
  // Request notification permission + start proactive alert stream
  requestNotificationPermission();
  startProactiveAlerts();
  if (state.opsAccess) {
    refreshStats();
    refreshTools();
  } else {
    refreshTools();
  }
}

/* ===== Sessions Sidebar ===== */
function _sessionDateLabel(isoStr) {
  if (!isoStr) return "";
  try {
    var d = new Date(isoStr);
    var now = new Date();
    var diffDays = Math.floor((now - d) / 86400000);
    if (diffDays === 0) return "Hoje";
    if (diffDays === 1) return "Ontem";
    if (diffDays <= 6) return "Últimos 7 dias";
    if (diffDays <= 29) return "Últimos 30 dias";
    return d.toLocaleDateString("pt-BR", { month: "short", year: "numeric" });
  } catch(e) { return ""; }
}
function _sessionTimeStr(isoStr) {
  if (!isoStr) return "";
  try {
    var d = new Date(isoStr);
    var now = new Date();
    var diffDays = Math.floor((now - d) / 86400000);
    if (diffDays === 0) return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
    if (diffDays <= 6) return d.toLocaleDateString("pt-BR", { weekday: "short" });
    return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
  } catch(e) { return ""; }
}
function renderSessions() {
  var el = $("session-list");
  if (!state.profile) { el.innerHTML = ""; return; }
  if (isPrivateMode()) {
    el.innerHTML = '<span class="dim" style="padding:6px 0;display:block;font-size:.75rem;">Modo privado ativo</span>';
    return;
  }
  var sessions = state.sessions || [];
  if (!sessions.length) {
    el.innerHTML = '<span class="dim" style="padding:6px 0;display:block;font-size:.75rem;">Nenhuma conversa salva</span>';
    return;
  }
  var html = "";
  var lastGroup = "";
  sessions.forEach(function(session) {
    var group = _sessionDateLabel(session.updated_at);
    if (group !== lastGroup) {
      html += '<div class="session-group-label">' + esc(group) + '</div>';
      lastGroup = group;
    }
    var isActive = session.session_id === state.sessionId;
    var title = session.title || "Nova conversa";
    var timeStr = _sessionTimeStr(session.updated_at);
    html += '<button class="session-btn' + (isActive ? ' active' : '') + '" data-sid="' + esc(session.session_id) + '">' +
      '<div class="session-btn-row">' +
        '<span class="session-title">' + esc(title) + '</span>' +
        (timeStr ? '<span class="session-time">' + esc(timeStr) + '</span>' : '') +
      '</div>' +
    '</button>';
  });
  el.innerHTML = html;
}
