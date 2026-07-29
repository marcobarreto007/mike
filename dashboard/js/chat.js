'use strict';
/* Mike Dashboard - Chat Rendering, Streaming, Image Upload, Composer */

/* ===== Chat Rendering ===== */
function renderSuggestions() {
  $("suggestions").innerHTML = SUGGESTIONS.map(function(s) {
    return '<button class="chip" data-prompt="' + esc(s.prompt) + '">' + s.icon + " " + esc(s.label) + "</button>";
  }).join("");
}

function renderChat() {
  var msgs = $("chat-msgs");
  msgs.innerHTML = "";
  var welcome = $("chat-welcome");
  if (!state.transcript.length) {
    welcome.hidden = false;
    return;
  }
  welcome.hidden = true;
  for (var i = 0; i < state.transcript.length; i++) {
    var m = state.transcript[i];
    appendMsg(m.role === "assistant" ? "ai" : "user", m.text, m.images, m.tools);
  }
  scrollDown();
}

function appendMsg(role, text, images, tools) {
  $("chat-welcome").hidden = true;
  var msgs = $("chat-msgs");
  var div = document.createElement("div");
  div.className = "msg";

  var av = document.createElement("div");
  av.className = "msg-av " + role;
  av.textContent = role === "ai" ? "M" : (PROFILES[state.profile] ? PROFILES[state.profile].name[0] : "U");

  var body = document.createElement("div");
  body.className = "msg-body";

  if (images && images.length) {
    var row = document.createElement("div");
    row.className = "msg-images";
    for (var i = 0; i < images.length; i++) {
      var img = document.createElement("img");
      img.src = imageUrl(images[i]);
      img.alt = "Foto anexada";
      row.appendChild(img);
    }
    body.appendChild(row);
  }

  if (text) {
    var el = document.createElement("div");
    el.innerHTML = role === "ai" ? renderMd(text) : "<p>" + esc(text) + "</p>";
    body.appendChild(el);
  }

  if (tools && tools.length) {
    var tEl = document.createElement("div");
    tEl.className = "msg-tools";
    for (var j = 0; j < tools.length; j++) {
      var pill = document.createElement("span");
      pill.className = "tool-pill " + (tools[j].ok ? "ok" : "fail");
      pill.textContent = (tools[j].ok ? "✓ " : "✗ ") + tools[j].name;
      tEl.appendChild(pill);
    }
    body.appendChild(tEl);
  }

  div.appendChild(av);
  div.appendChild(body);
  msgs.appendChild(div);
  scrollDown();
  return body;
}

function scrollDown() {
  var el = $("chat-scroll");
  el.scrollTop = el.scrollHeight;
}

function showTyping() {
  var body = appendMsg("ai", "");
  body.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div><span class="typing-label">Pensando...</span>';
  return body;
}

/* ===== Image Upload ===== */
async function handleImageSelect(e) {
  var files = Array.prototype.slice.call(e.target.files || []);
  e.target.value = "";
  clearComposerNote();

  var imageFiles = [];
  for (var i = 0; i < files.length; i++) {
    if (files[i].type && files[i].type.indexOf("image/") === 0) imageFiles.push(files[i]);
  }
  if (!imageFiles.length) return;

  if (imageFiles.length > FRONTEND_VISION_MAX_IMAGES) {
    setComposerNote("Por enquanto o Mike aceita 1 foto por envio. Mantive so a primeira.", "warn");
  }

  try {
    var optimized = await optimizeImageFile(imageFiles[0]);
    state.images = [optimized];
    renderImgPreview();
  } catch (err) {
    state.images = [];
    renderImgPreview();
    setComposerNote(err.message || "Nao consegui preparar a foto para envio.", "error");
  }
}

function renderImgPreview() {
  var el = $("img-preview");
  if (!state.images.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML = "";
  for (var i = 0; i < state.images.length; i++) {
    var thumb = document.createElement("div");
    thumb.className = "thumb";
    var img = document.createElement("img");
    img.src = imageUrl(state.images[i]);
    img.alt = "Preview " + (i + 1);
    var btn = document.createElement("button");
    btn.className = "rm-img";
    btn.type = "button";
    btn.dataset.idx = i;
    btn.textContent = "✕";
    thumb.appendChild(img);
    thumb.appendChild(btn);
    el.appendChild(thumb);
  }
}

/* ===== Send Message ===== */
async function sendMessage(prefill) {
  // Conversational identification flow
  if (state.identifying) {
    if (state.streaming) return;
    var idText = (prefill || $('chat-input').value).trim();
    if (!idText) return;
    $('chat-input').value = '';
    $('chat-input').style.height = 'auto';
    appendMsg('user', idText);
    state.identifyAttempts++;
    // Ask server — keywords never exposed in frontend
    try {
      var result = await api('/v1/auth/identify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: idText }),
      });
      if (result && result.profile) {
        state.identifying = false;
        setTimeout(function() { startSession(result.profile); }, 200);
        // Restore suggestions after login
        /* $('suggestions').hidden = false; — removed */
        return;
      }
    } catch (e) { /* server error — fall through */ }
    if (state.identifyAttempts >= 2) {
      state.identifying = false;
      /* $('suggestions').hidden = false; — removed */
      setTimeout(function() { startSession('visitante'); }, 200);
    } else {
      setTimeout(function() {
        appendMsg('ai', 'Nao te reconheci ainda... pode repetir seu nome ou apelido?');
        $('chat-input').focus();
      }, 400);
    }
    return;
  }
  if (!state.profile || state.streaming) return;
  var text = prefill || $("chat-input").value.trim();
  var images = state.images.slice();
  if (!text && !images.length) return;

  $("chat-input").value = "";
  $("chat-input").style.height = "auto";
  state.images = [];
  renderImgPreview();
  clearComposerNote();

  var userText = text;
  if (images.length) userText += "\n(" + images.length + " foto(s) anexada(s))";
  var privateMode = isPrivateMode();
  var useStreaming = !images.length && state.streamSupport.text;
  appendMsg("user", text, images);
  state.transcript.push({ role: "user", text: userText, images: images, at: new Date().toISOString() });

  var botBody = showTyping();
  setStreaming(true);
  var fullText = "";
  var streamError = null;
  var abortedPrivateTurn = false;

  try {
    state.abort = new AbortController();

    // Build message content: string if text-only, array if images attached
    var msgContent;
    if (images.length) {
      msgContent = [];
      if (text) msgContent.push({ type: "text", text: text });
      for (var im = 0; im < images.length; im++) {
        msgContent.push({ type: "image_url", image_url: { url: imageUrl(images[im]) } });
      }
    } else {
      msgContent = text || "(sem texto)";
    }

    // Eternal Memory: include last 100 messages of history for 128k cloud context
    var historyMessages = (state.transcript || []).slice(-100).map(function(m) {
      return { role: m.role === "assistant" ? "assistant" : "user", content: m.text || "" };
    });
    // Append current message
    historyMessages.push({ role: "user", content: msgContent });

    var body = {
      model: "mike",
      session_id: state.sessionId,
      private_mode: privateMode,
      messages: historyMessages,
      max_tokens: 2048,
      stream: useStreaming,
    };

    if (useStreaming) {
      // Auto-timeout: abort if no response starts within 300s
      var _fetchTimeout = setTimeout(function() {
        if (state.abort) state.abort.abort();
      }, 300000);
      var resp = await fetch(API + "/v1/chat/completions", {
        method: "POST",
        credentials: "same-origin",
        headers: headers({ "Content-Type": "application/json", Accept: "text/event-stream" }),
        signal: state.abort.signal,
        body: JSON.stringify(body),
      });
      clearTimeout(_fetchTimeout);

      if (resp.status === 401) throw new Error("Sessao expirada. Entre novamente.");
      if (!resp.ok) {
        var errPayload = await resp.json().catch(function() { return null; });
        throw new Error(errPayload && errPayload.error ? errPayload.error : ("Erro " + resp.status));
      }

      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      var firstToken = true;

      while (true) {
        var chunk = await reader.read();
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        var idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          var part = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          var lines = part.split("\n");
          for (var li = 0; li < lines.length; li++) {
            var ln = lines[li].trim();
            if (ln === "data: [DONE]") break;
            if (ln.indexOf("data: ") !== 0) continue;
            try {
              var d = JSON.parse(ln.slice(6));
              if (d.error && d.error.error) {
                streamError = d.error.error;
                fullText = streamError;
                botBody.innerHTML = '<p style="color:var(--danger)">' + esc(fullText) + "</p>";
                scrollDown();
                continue;
              }
              var delta = d.choices && d.choices[0] && d.choices[0].delta ? d.choices[0].delta.content || "" : "";
              if (delta) {
                if (firstToken) {
                  // Remove dots, update label from "Pensando..." to "Escrevendo..."
                  var dots = botBody.querySelector('.typing-dots');
                  if (dots) dots.remove();
                  var label = botBody.querySelector('.typing-label');
                  if (label) { label.textContent = 'Escrevendo...'; label.classList.add('writing'); }
                  // Create content wrapper for streamed text
                  var wrapper = document.createElement('div');
                  wrapper.className = 'stream-content';
                  botBody.appendChild(wrapper);
                  firstToken = false;
                }
                fullText += delta;
                var wrapper = botBody.querySelector('.stream-content');
                if (wrapper) wrapper.innerHTML = renderMd(fullText);
                else botBody.innerHTML = renderMd(fullText);
                scrollDown();
              }
            } catch (pe) {}
          }
        }
        if (chunk.done) break;
      }
      if (!fullText) fullText = streamError || "(sem resposta)";
      if (streamError) botBody.innerHTML = '<p style="color:var(--danger)">' + esc(fullText) + "</p>";
      else botBody.innerHTML = renderMd(fullText);
    } else {
      var data = await api("/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: state.abort.signal,
        body: JSON.stringify(body),
      });
      fullText = data && typeof data.assistant_text === "string"
        ? data.assistant_text
        : (data && data.choices && data.choices[0] && data.choices[0].message
          ? (data.choices[0].message.content || "")
          : "(sem resposta)");
      botBody.innerHTML = renderMd(fullText || "(sem resposta)");
    }
  } catch (err) {
    if (err.name === "AbortError") {
      if (privateMode) {
        abortedPrivateTurn = true;
      } else {
        fullText = fullText || "Geracao interrompida.";
        botBody.innerHTML = renderMd(fullText);
      }
    } else {
      fullText = err.message;
      botBody.innerHTML = '<p style="color:var(--danger)">' + esc(fullText) + "</p>";
    }
  } finally {
    state.abort = null;
    setStreaming(false);
    $("chat-input").focus();
    if (abortedPrivateTurn) {
      state.sessionId = createSessionId(state.profile || "main");
      state.transcript = [];
      state.sessions = [];
      renderChat();
      renderSessions();
    } else {
      state.transcript.push({ role: "assistant", text: fullText, at: new Date().toISOString() });
      if (!privateMode) {
        await loadSessions();
      } else {
        state.sessions = [];
        renderSessions();
      }
    }
    $("user-session").textContent = state.sessionId ? state.sessionId.slice(0, 20) : "-";
    updatePrivacyUi();
    if (state.opsAccess) refreshStats();
  }
}

function stopStream() { if (state.abort) state.abort.abort(); }

function setStreaming(on) {
  state.streaming = on;
  $("send-btn").disabled = on;
  $("send-btn").hidden = on;
  $("stop-btn").hidden = !on;
  // Yorkie thinking animation
  var faces = document.querySelectorAll("#login-yorkie, #welcome-yorkie");
  for (var i = 0; i < faces.length; i++) {
    faces[i].classList.toggle("yorkie-thinking", on);
  }
}

/* ===== Composer ===== */
function autoResize() {
  var el = $("chat-input");
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 150) + "px";
}
