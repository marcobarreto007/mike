/* Mike Dashboard - Push Notifications & Proactive Alerts */

var _notifEventSource = null;

function stopProactiveAlerts() {
  if (_notifEventSource) {
    _notifEventSource.close();
    _notifEventSource = null;
  }
}

function startProactiveAlerts() {
  stopProactiveAlerts();
  if (!state.profile || state.profile === "visitante") return;
  try {
    _notifEventSource = new EventSource(API + "/v1/notifications/stream", { withCredentials: true });
    _notifEventSource.addEventListener("alert", function(e) {
      try {
        var data = JSON.parse(e.data);
        showProactiveNotification(data.title || "Mike", data.body || data.message || "Novo aviso.");
      } catch (_) {}
    });
    _notifEventSource.onerror = function() {
      // Silently retry — EventSource auto-reconnects
    };
  } catch (_) {}
}

function showProactiveNotification(title, body) {
  // Try native Notification API
  if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    var n = new Notification(title, {
      body: body,
      icon: "/static/icons/mike-icon-192.png",
      badge: "/static/icons/mike-icon-64.png",
      tag: "mike-proativo",
    });
    n.onclick = function() { window.focus(); n.close(); };
  } else {
    // Fallback: show in the chat as a system message
    appendMsg("ai", "⚠️ **" + title + "** — " + body);
    scrollChat();
  }
  // Also post to service worker for background display
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({
      type: "MIKE_NOTIFY",
      title: title,
      body: body,
    });
  }
}

async function requestNotificationPermission() {
  if (typeof Notification === "undefined") return;
  if (Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch (_) {}
  }
}
