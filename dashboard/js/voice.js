/* Mike Dashboard - Voice: Speech Recognition (mic) + TTS (speaker) */

var _micRecognition = null;
var _micListening = false;
var _ttsAudio = null;
var _ttsSpeakingBtn = null;

function isSpeechRecognitionSupported() {
  return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
}

function toggleMic() {
  if (!isSpeechRecognitionSupported()) {
    setComposerNote("Seu navegador nao suporta reconhecimento de voz. Use o Chrome.", "error");
    return;
  }
  if (_micListening) {
    stopMic();
    return;
  }
  startMic();
}

function startMic() {
  if (_micListening) return;
  // Request microphone permission first (needed for Android WebView / Capacitor)
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function(stream) {
      stream.getTracks().forEach(function(t) { t.stop(); }); // release immediately
      _startMicReal();
    }).catch(function(err) {
      setComposerNote("Permissão do microfone negada: " + err.message, "error");
    });
  } else {
    _startMicReal();
  }
}

function _startMicReal() {
  if (_micListening) return;
  var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  _micRecognition = new SpeechRecognition();
  _micRecognition.lang = "pt-BR";
  _micRecognition.continuous = true;
  _micRecognition.interimResults = true;
  _micRecognition.maxAlternatives = 1;

  var finalTranscript = "";
  var silenceTimer = null;

  _micRecognition.onstart = function() {
    _micListening = true;
    var btn = $("mic-btn");
    if (btn) btn.classList.add("listening");
    $("chat-input").placeholder = "Escutando... fale agora";
  };

  _micRecognition.onresult = function(e) {
    var interim = "";
    for (var i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) {
        finalTranscript += e.results[i][0].transcript + " ";
      } else {
        interim += e.results[i][0].transcript;
      }
    }
    $("chat-input").value = finalTranscript + interim;
    autoResize();

    // Reset silence timer — auto-send after 2s of silence
    if (silenceTimer) clearTimeout(silenceTimer);
    if (finalTranscript.trim()) {
      silenceTimer = setTimeout(function() {
        stopMic();
        if ($("chat-input").value.trim()) {
          sendMessage();
        }
      }, 2500);
    }
  };

  _micRecognition.onerror = function(e) {
    if (e.error !== "aborted" && e.error !== "no-speech") {
      setComposerNote("Erro no microfone: " + e.error, "error");
    }
    stopMic();
  };

  _micRecognition.onend = function() {
    // If still in listening mode but ended unexpectedly, restart
    if (_micListening && finalTranscript.trim()) {
      stopMic();
      if ($("chat-input").value.trim()) {
        sendMessage();
      }
    } else {
      stopMic();
    }
  };

  try {
    _micRecognition.start();
  } catch (err) {
    setComposerNote("Nao consegui acessar o microfone.", "error");
    stopMic();
  }
}

function stopMic() {
  _micListening = false;
  var btn = $("mic-btn");
  if (btn) btn.classList.remove("listening");
  $("chat-input").placeholder = "Mensagem para o Mike...";
  if (_micRecognition) {
    try { _micRecognition.abort(); } catch (e) {}
    _micRecognition = null;
  }
}

function speakLastResponse() {
  // Find the last AI message text from transcript
  var lastAi = "";
  for (var i = state.transcript.length - 1; i >= 0; i--) {
    if (state.transcript[i].role === "assistant" && state.transcript[i].text) {
      lastAi = state.transcript[i].text;
      break;
    }
  }
  if (!lastAi) {
    setComposerNote("Nenhuma resposta do Mike para ouvir.", "warn");
    return;
  }
  speakText(lastAi, $("speak-btn"));
}

function speakText(text, btn) {
  // Stop any current playback
  if (_ttsAudio) {
    _ttsAudio.pause();
    _ttsAudio.currentTime = 0;
    _ttsAudio = null;
    if (_ttsSpeakingBtn) {
      _ttsSpeakingBtn.classList.remove("speaking");
      _ttsSpeakingBtn.innerHTML = "🔊";
    }
    // If clicking same button, just stop
    if (_ttsSpeakingBtn === btn) {
      _ttsSpeakingBtn = null;
      return;
    }
  }

  // Strip markdown for cleaner speech
  var cleanText = text
    .replace(/```[\s\S]*?```/g, " codigo omitido ")
    .replace(/`[^`]+`/g, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/#{1,6}\s*/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[\n\r]+/g, ". ")
    .replace(/\s+/g, " ")
    .trim();

  if (!cleanText) return;

  _ttsSpeakingBtn = btn;
  btn.classList.add("speaking");
  btn.innerHTML = "⏹";

  fetch(API + "/v1/tts", {
    method: "POST",
    credentials: "same-origin",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ text: cleanText }),
  })
  .then(function(resp) {
    if (!resp.ok) throw new Error("TTS error " + resp.status);
    return resp.blob();
  })
  .then(function(blob) {
    var url = URL.createObjectURL(blob);
    _ttsAudio = new Audio(url);
    _ttsAudio.playbackRate = 1.05;
    _ttsAudio.onended = function() {
      btn.classList.remove("speaking");
      btn.innerHTML = "🔊";
      _ttsAudio = null;
      _ttsSpeakingBtn = null;
      URL.revokeObjectURL(url);
    };
    _ttsAudio.onerror = function() {
      btn.classList.remove("speaking");
      btn.innerHTML = "🔊";
      _ttsAudio = null;
      _ttsSpeakingBtn = null;
    };
    _ttsAudio.play();
  })
  .catch(function(err) {
    btn.classList.remove("speaking");
    btn.innerHTML = "🔊";
    _ttsSpeakingBtn = null;
    setComposerNote("Nao consegui gerar a voz: " + err.message, "error");
  });
}
