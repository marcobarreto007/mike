/* Mike Dashboard - Status Page & Stats */

/* ===== Status Page ===== */
var _statusInterval = null;

async function refreshStatusPage() {
  var now = new Date().toLocaleTimeString('pt-BR');
  var lastEl = $('status-last-update');
  if (lastEl) lastEl.textContent = 'Atualizado às ' + now;

  try {
    var s = await api('/stats');

    // Servidor
    kv('sv-status', [
      ['Porta', s.port || 8083],
      ['Host', s.host || '127.0.0.1'],
      ['Requests', s.total_requests || 0],
      ['Tokens gerados', s.total_tokens_generated || 0],
      ['Conversas', s.conversation_records || 0],
    ]);

    // Modelo
    kv('sv-model', [
      ['Backend', s.llm_backend || s.runtime_profile || '-'],
      ['Modelo', s.model_file || s.model_name || s.deepseek_model || '-'],
      ['Contexto', s.ctx_size || '-'],
      ['Max tokens', s.default_max_tokens || '-'],
      ['GPU Layers', s.gpu_layers || '-'],
    ]);

    // GPU
    kv('sv-gpu', [
      ['GPU', s.gpu_name || 'N/A'],
      ['VRAM livre', s.gpu_memory_free_mb ? s.gpu_memory_free_mb + ' MB' : 'N/A'],
      ['VRAM total', s.gpu_memory_total_mb ? s.gpu_memory_total_mb + ' MB' : 'N/A'],
      ['Vision', s.vision_enabled ? (s.vision_runtime_profile || 'on') : 'off'],
    ]);

    // Memória / Conhecimento
    kv('sv-memory', [
      ['Knowledge', (s.knowledge_documents || 0) + ' docs / ' + (s.knowledge_chunks || 0) + ' chunks'],
      ['Web Search', (s.web_search_provider || '-') + (s.web_search_ready ? ' ✅' : ' ❌')],
      ['Backups', s.backup_archives || 0],
      ['Roadmap', (s.roadmap_items_completed || 0) + '/' + (s.roadmap_items_total || 0)],
    ]);

    // Tools
    kv('sv-tools', [
      ['Total tools', s.mcp_tool_count || 0],
      ['Email', s.mcp_email_enabled ? '✅ ativo' : '❌ off'],
      ['Agenda', s.mcp_calendar_enabled ? '✅ ativo' : '❌ off'],
      ['Planilhas', s.mcp_spreadsheet_enabled ? '✅ ativo' : '❌ off'],
      ['Brave API', s.brave_api_key_present ? '✅' : '❌ sem chave'],
    ]);
  } catch (e) {
    ['sv-status','sv-model','sv-gpu','sv-memory','sv-tools'].forEach(function(id) {
      var el = $(id); if (el) el.innerHTML = '<span class="dim">Offline</span>';
    });
  }

  // Autonomia
  try {
    var a = await api('/v1/autonomy/status');
    var routinesRaw = a && a.routines ? a.routines : [];
    var routines = Array.isArray(routinesRaw) ? routinesRaw.slice(0, 4) : [];
    var rows = [];

    if (routines.length) {
      rows = routines.map(function(r) {
        var ok = r.fail_count === 0 || r.run_count > r.fail_count ? '✅' : '⚠️';
        return [r.name || r.id, ok + ' runs:' + (r.run_count||0) + ' erros:' + (r.fail_count||0)];
      });
    } else if (routinesRaw && typeof routinesRaw === 'object') {
      rows.push(['Rotinas', (routinesRaw.enabled || 0) + '/' + (routinesRaw.total || 0) + ' ativas']);
    }

    if (a && a.tasks && typeof a.tasks === 'object') {
      rows.push(['Tasks', (a.tasks.running || 0) + ' rodando / ' + (a.tasks.pending || 0) + ' pendentes']);
    }
    if (a && typeof a.running === 'boolean') {
      rows.unshift(['Engine', a.running ? '✅ ativa' : '⚠️ parada']);
    }
    if (!rows.length) rows = [['Rotinas', 'Nenhuma ativa']];
    rows.push(['Tick', (a.tick_seconds || 60) + 's']);
    kv('sv-autonomy', rows);
  } catch(e) {
    var el = $('sv-autonomy'); if (el) el.innerHTML = '<span class="dim">Indisponível</span>';
  }
}

function kv(elId, pairs) {
  var el = $(elId);
  if (!el) return;
  el.innerHTML = pairs.map(function(p) {
    return '<div class="skv"><span class="skv-k">' + esc(p[0]) + '</span><span class="skv-v">' + esc(String(p[1])) + '</span></div>';
  }).join('');
}

/* ===== Tools: Stats ===== */
async function refreshStats() {
  if (!state.opsAccess) {
    $("stats-grid").innerHTML = '<span class="dim">Disponivel apenas para owner/local.</span>';
    return;
  }
  try {
    var s = await api("/stats");
    state.stats = s;
    $("stats-grid").innerHTML = [
      ["GPU", s.gpu_name || "N/A"],
      ["VRAM", s.gpu_memory_free_mb ? s.gpu_memory_free_mb + "/" + s.gpu_memory_total_mb + " MB" : "N/A"],
      ["Modelo", s.model_file || s.model_name || "-"],
      ["GPU Layers", s.gpu_layers || "-"],
      ["Contexto", s.ctx_size || "-"],
      ["Max tokens", s.default_max_tokens || "-"],
      ["Runtime", s.runtime_profile || "-"],
      ["Keepalive", s.stream_keepalive_seconds ? (s.stream_keepalive_seconds + "s") : "-"],
      ["Timeout web", s.web_request_timeout_seconds === 0 ? "sem timeout" : (s.web_request_timeout_seconds || "-")],
      ["Vision", s.vision_enabled ? (s.vision_runtime_profile || "on") : "off"],
      ["Limite foto", s.vision_max_decoded_bytes ? ((s.vision_max_decoded_bytes / (1024 * 1024)).toFixed(1) + " MiB / " + (s.vision_max_images || 0) + " img") : "-"],
      ["Requests", s.total_requests || 0],
      ["Tokens", s.total_tokens_generated || 0],
      ["Conversas", s.conversation_records || 0],
      ["Knowledge", (s.knowledge_documents || 0) + " docs / " + (s.knowledge_chunks || 0) + " chunks"],
      ["Web Search", (s.web_search_provider || "-") + (s.web_search_ready ? " (ready)" : "")],
      ["MCP Tools", s.mcp_tool_count || 0],
      ["Email MCP", s.mcp_email_enabled ? "ativo" : "off"],
      ["Agenda MCP", s.mcp_calendar_enabled ? "ativo" : "off"],
      ["Planilhas MCP", s.mcp_spreadsheet_enabled ? "ativo" : "off"],
      ["Backups", s.backup_archives || 0],
      ["Roadmap", (s.roadmap_items_completed || 0) + "/" + (s.roadmap_items_total || 0)],
    ].map(function(pair) {
      return '<div class="stat-item"><div class="stat-label">' + esc(pair[0]) +
        '</div><div class="stat-value">' + esc(String(pair[1])) + "</div></div>";
    }).join("");
  } catch (e) { $("stats-grid").innerHTML = '<span class="dim">Offline</span>'; }
}
