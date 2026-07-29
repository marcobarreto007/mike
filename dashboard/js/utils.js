/* Mike Dashboard - Utilities, Constants, and Shared Helpers */
const _MIKE_DEFAULT_SERVER = "";                        // empty = same origin
const API = (function() {
  // Capacitor / mobile: use stored server URL
  if (window.Capacitor) return localStorage.getItem("mike_server_url") || _MIKE_DEFAULT_SERVER;
  // Custom override via localStorage
  var stored = localStorage.getItem("mike_server_url");
  if (stored) return stored;
  // Normal browser / PWA: same origin
  return location.origin;
})();
const $ = (id) => document.getElementById(id);

/* ===== State ===== */
const state = {
  profile: null,
  sessionId: null,
  sessions: [],
  transcript: [],
  images: [],
  streaming: false,
  abort: null,
  page: "chat",
  bootstrap: null,
  toolSummary: null,
  opsAccess: false,
  privateMode: false,
  streamSupport: { text: true, vision: false },
  stats: null,
  tools: null,
  identifying: false,
  identifyAttempts: 0,
  installPrompt: null,
};

const PROFILES = {
  marco:     { name: "Marco",     greeting: "Fala, Marco! O que vamos construir hoje?" },
  anapaula:  { name: "Ana Paula", greeting: "Ola, Ana Paula! Como posso ajudar?" },
  raphael:   { name: "Raphael",   greeting: "Ciao, Raphael! Pronto para pesquisa." },
  alice:     { name: "Alice",     greeting: "Oi, Alice! No que posso te ajudar?" },
  matheus:   { name: "Matheus",   greeting: "Oi, Matheus! Pronto para ajudar." },
  visitante: { name: "Visitante", greeting: "Oi! Sessao temporaria. Memoria longa desativada." },
};

const SUGGESTIONS = [
  { icon: "💡", label: "Analise o projeto",      prompt: "Analise a estrutura do projeto e me de um resumo" },
  { icon: "🔍", label: "Pesquisa na web",        prompt: "Pesquise na internet: " },
  { icon: "📝", label: "Escreva codigo",         prompt: "Escreva um " },
  { icon: "📁", label: "Ver workspace",          prompt: "Liste os arquivos do workspace" },
];

const FRONTEND_VISION_MAX_IMAGES = 1;
const FRONTEND_VISION_MAX_DECODED_BYTES = 2 * 1024 * 1024;
const FRONTEND_VISION_MAX_DIMENSION = 1280;
const FRONTEND_VISION_JPEG_QUALITY = 0.78;
const WINDOWS_SHORTCUT_PATH = "/download/mike.url";
const INSTALL_CARD_STATE_KEY = "mike_install_card_state";

/* ===== Utils ===== */
function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function toolHasCapability(tool, cap) {
  var wanted = String(cap || "").toLowerCase();
  if (!tool || !wanted) return false;
  var caps = tool.capabilities || [];
  for (var i = 0; i < caps.length; i++) {
    if (String(caps[i]).toLowerCase() === wanted) return true;
  }
  return false;
}

function toolsHaveCapability(cap) {
  var summary = state.tools || state.toolSummary || {};
  if (cap === "email" && summary.email_enabled) return true;
  if (cap === "calendar" && summary.calendar_enabled) return true;
  if ((cap === "spreadsheet" || cap === "excel") && summary.spreadsheet_enabled) return true;
  var tools = (state.tools && state.tools.tools) || [];
  for (var i = 0; i < tools.length; i++) {
    if (toolHasCapability(tools[i], cap)) return true;
  }
  return false;
}

function renderMd(text) {
  if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
    marked.setOptions({ breaks: true, gfm: true });
    return DOMPurify.sanitize(marked.parse(text));
  }
  /* fallback */
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\n/g, "<br>");
}

function headers(extra) {
  return { ...extra };
}

async function api(path, opts) {
  opts = opts || {};
  const resp = await fetch(API + path, {
    credentials: "same-origin", ...opts,
    headers: headers(opts.headers || {}),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(function() { return {}; });
    const err = new Error(body.error || "HTTP " + resp.status);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

function createSessionId(profile) {
  // Stable session per profile — preserves memory across logins
  if (profile && profile !== "visitante") return profile + "-main";
  return (profile || "visitante") + "-" + Date.now();
}

function isPrivateMode() {
  return state.profile === "visitante" || !!state.privateMode;
}

function isProfileAuthDisabled() {
  return !!(state.bootstrap && state.bootstrap.profile_auth_enabled === false);
}

function updatePrivacyUi() {
  var toggle = $("privacy-toggle");
  var label = $("privacy-label");
  if (!toggle || !label) return;
  var forcedPrivate = state.profile === "visitante";
  toggle.checked = forcedPrivate || !!state.privateMode;
  toggle.disabled = forcedPrivate || state.streaming || !state.profile;
  label.textContent = toggle.checked
    ? "Modo privado: sem salvar no Mike"
    : "Salvar conversa no Mike";
}

function applyOpsAccess() {
  var opsIds = ["ops-workspace-section", "ops-system-section", "ops-runner-section"];
  for (var i = 0; i < opsIds.length; i++) {
    var el = $(opsIds[i]);
    if (el) el.hidden = !state.opsAccess;
  }
}

function applyBootstrap(data) {
  state.bootstrap = data || null;
  state.toolSummary = data && data.tool_summary ? data.tool_summary : { tool_count: 0 };
  state.opsAccess = !!(data && data.ops_access);
  state.streamSupport = {
    text: !!(data && data.chat && data.chat.supports_text_streaming),
    vision: !!(data && data.chat && data.chat.supports_vision_streaming),
  };
  applyOpsAccess();
  updatePrivacyUi();
}

async function refreshBootstrap() {
  var data = await api("/v1/client/bootstrap");
  applyBootstrap(data);
  return data;
}

function imageUrl(image) {
  if (!image) return "";
  if (typeof image === "string") return image;
  return image.url || image.dataUrl || "";
}

function setComposerNote(message, tone) {
  var el = $("composer-note");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.className = "composer-note";
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.className = "composer-note" + (tone ? " " + tone : "");
  el.textContent = message;
}

function clearComposerNote() {
  setComposerNote("");
}

function readFileAsDataUrl(file) {
  return new Promise(function(resolve, reject) {
    var reader = new FileReader();
    reader.onload = function() { resolve(reader.result); };
    reader.onerror = function() { reject(new Error("Nao consegui ler a foto selecionada.")); };
    reader.readAsDataURL(file);
  });
}

function loadHtmlImage(dataUrl) {
  return new Promise(function(resolve, reject) {
    var img = new Image();
    img.onload = function() { resolve(img); };
    img.onerror = function() { reject(new Error("Nao consegui abrir a foto selecionada.")); };
    img.src = dataUrl;
  });
}

function canvasToBlob(canvas, type, quality) {
  return new Promise(function(resolve, reject) {
    if (!canvas.toBlob) {
      reject(new Error("Seu navegador nao conseguiu preparar a foto para envio."));
      return;
    }
    canvas.toBlob(function(blob) {
      if (blob) resolve(blob);
      else reject(new Error("Nao consegui comprimir a foto para envio."));
    }, type, quality);
  });
}

async function optimizeImageFile(file) {
  var originalDataUrl = await readFileAsDataUrl(file);
  var img = await loadHtmlImage(originalDataUrl);
  var srcWidth = img.naturalWidth || img.width || 1;
  var srcHeight = img.naturalHeight || img.height || 1;
  var scale = Math.min(1, FRONTEND_VISION_MAX_DIMENSION / Math.max(srcWidth, srcHeight));
  var targetWidth = Math.max(1, Math.round(srcWidth * scale));
  var targetHeight = Math.max(1, Math.round(srcHeight * scale));

  var canvas = document.createElement("canvas");
  canvas.width = targetWidth;
  canvas.height = targetHeight;

  var ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Nao consegui preparar a foto para envio.");
  ctx.drawImage(img, 0, 0, targetWidth, targetHeight);

  var blob = await canvasToBlob(canvas, "image/jpeg", FRONTEND_VISION_JPEG_QUALITY);
  if (blob.size > FRONTEND_VISION_MAX_DECODED_BYTES) {
    throw new Error("A foto ainda ficou acima de 2 MiB mesmo apos comprimir. Tente aproximar ou cortar a imagem.");
  }

  return {
    url: await readFileAsDataUrl(blob),
    sizeBytes: blob.size,
    mimeType: "image/jpeg",
    width: targetWidth,
    height: targetHeight,
  };
}

function isStandaloneMode() {
  return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function isWindowsDevice() {
  return /windows/i.test(navigator.userAgent || "");
}

function isAppleMobileDevice() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent || "");
}

function isInstallCardDismissed() {
  try {
    return localStorage.getItem(INSTALL_CARD_STATE_KEY) === "installed";
  } catch (err) {
    return false;
  }
}

function markInstallCardInstalled() {
  try {
    localStorage.setItem(INSTALL_CARD_STATE_KEY, "installed");
  } catch (err) {}
}

function installFallbackMessage() {
  if (isStandaloneMode()) {
    return "Mike ja esta instalado neste aparelho. Agora e so abrir pelo icone.";
  }
  if (isAppleMobileDevice()) {
    return "No iPhone ou iPad, toque em Compartilhar e depois em Adicionar a Tela de Inicio.";
  }
  if (/android/i.test(navigator.userAgent || "")) {
    return "No Android, abra o menu do navegador e toque em Instalar app ou Adicionar a tela inicial.";
  }
  return "No Chrome ou Edge, instale o Mike como app. Se preferir, baixe o atalho do Windows e arraste para a area de trabalho.";
}

function refreshInstallCard() {
  var card = $("install-card");
  var installBtn = $("install-app-btn");
  var shortcutBtn = $("download-shortcut-btn");
  var status = $("install-status");
  var pill = $("install-pill");
  if (!card || !installBtn || !shortcutBtn || !status || !pill) return;

  var standalone = isStandaloneMode();
  var installed = standalone || isInstallCardDismissed();
  card.hidden = installed;
  card.classList.toggle("installed", standalone);
  pill.textContent = standalone ? "Instalado" : "Atalho facil";

  if (installed) {
    installBtn.hidden = true;
    shortcutBtn.hidden = true;
    status.textContent = "Pronto. O Mike agora pode ser aberto direto pelo icone.";
    return;
  }

  installBtn.hidden = !state.installPrompt;
  installBtn.disabled = !state.installPrompt;
  shortcutBtn.hidden = !isWindowsDevice();

  if (state.installPrompt) {
    status.textContent = "Clique em Instalar Mike. O navegador cria um app com icone bonito na area de trabalho.";
  } else {
    status.textContent = installFallbackMessage();
  }
}

async function handleInstallApp() {
  var status = $("install-status");
  if (!state.installPrompt) {
    refreshInstallCard();
    return;
  }
  try {
    var promptEvent = state.installPrompt;
    state.installPrompt = null;
    await promptEvent.prompt();
    if (promptEvent.userChoice) {
      var choice = await promptEvent.userChoice;
      if (choice && choice.outcome === "accepted") {
        markInstallCardInstalled();
      }
      if (status) {
        status.textContent = choice && choice.outcome === "accepted"
          ? "Instalacao pedida. Se o navegador confirmar, o Mike vai aparecer como app."
          : "Tudo bem. Se preferir, use o atalho do Windows.";
      }
    }
  } catch (err) {
    if (status) status.textContent = "Nao consegui abrir a instalacao agora. Tente de novo ou use o atalho do Windows.";
  } finally {
    refreshInstallCard();
  }
}

function handleShortcutDownload() {
  var status = $("install-status");
  window.location.href = WINDOWS_SHORTCUT_PATH;
  if (status) {
    status.textContent = "Atalho baixado. Agora e so arrastar o arquivo para a area de trabalho.";
  }
}
