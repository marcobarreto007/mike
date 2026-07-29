/* Mike Dashboard - Tool List, Runner, Skills, Match */

/* ===== Tools: Tool List & Runner ===== */
async function refreshTools() {
  var summary = state.toolSummary || {};
  var capabilityParts = [];
  if (summary.email_enabled) capabilityParts.push("email");
  if (summary.calendar_enabled) capabilityParts.push("agenda");
  if (summary.spreadsheet_enabled) capabilityParts.push("planilhas");
  $("tools-badge").textContent = (summary.tool_count || 0) + " tools" +
    (state.profile ? " (" + state.profile + ")" : "") +
    (capabilityParts.length ? " • " + capabilityParts.join(" + ") : "");
  var capEl = $("mcp-capabilities");
  if (capEl) {
    capEl.hidden = false;
    if (capabilityParts.length) {
      capEl.textContent = "MCP conectado: " + capabilityParts.join(" + ") + ". O Mike pode agir de verdade nessas ferramentas.";
    } else {
      capEl.textContent = "Nenhum MCP de email, agenda ou planilhas conectado ainda. O Mike pode rascunhar ou orientar, mas nao executar essas acoes fora do workspace.";
    }
  }
  if (!state.opsAccess) {
    state.tools = null;
    var selDisabled = $("tool-select");
    if (selDisabled) selDisabled.innerHTML = '<option value="">Owner/local apenas</option>';
    $("run-tool").disabled = true;
    return;
  }
  try {
    var data = await api("/v1/tools");
    state.tools = data;
    state.toolSummary = {
      tool_count: data.tool_count,
      email_enabled: data.email_enabled,
      calendar_enabled: data.calendar_enabled,
      spreadsheet_enabled: data.spreadsheet_enabled,
    };
    capabilityParts = [];
    if (data.email_enabled) capabilityParts.push("email");
    if (data.calendar_enabled) capabilityParts.push("agenda");
    if (data.spreadsheet_enabled) capabilityParts.push("planilhas");
    $("tools-badge").textContent = data.tool_count + " tools (" + (data.profile || "sem perfil") + ")" +
      (capabilityParts.length ? " • " + capabilityParts.join(" + ") : "");
    if (capEl) {
      capEl.hidden = false;
      if (data.email_enabled || data.calendar_enabled || data.spreadsheet_enabled) {
        capEl.textContent = "MCP conectado: " +
          [
            data.email_enabled ? "email" : null,
            data.calendar_enabled ? "agenda" : null,
            data.spreadsheet_enabled ? "planilhas" : null,
          ].filter(Boolean).join(" + ") +
          ". O Mike pode agir de verdade por essas ferramentas.";
      } else {
        capEl.textContent = "Nenhum MCP de email, agenda ou planilhas conectado ainda. O Mike pode rascunhar ou orientar, mas nao executar essas acoes fora do workspace.";
      }
    }
    var sel = $("tool-select");
    sel.innerHTML = '<option value="">Selecione uma tool...</option>';
    var tools = data.tools || [];
    for (var i = 0; i < tools.length; i++) {
      var opt = document.createElement("option");
      opt.value = tools[i].name;
      opt.textContent = tools[i].name + (tools[i].server_name && tools[i].server_name !== "workspace" ? " [" + tools[i].server_name + "]" : "");
      sel.appendChild(opt);
    }
    $("run-tool").disabled = true;
  } catch (e) { $("tools-badge").textContent = "Offline"; }
}

async function refreshSkills() {
  var list = $("skills-list");
  if (list) list.innerHTML = '<span class="dim">Buscando skills...</span>';
  try {
    var data = await api("/v1/skills");
    var skills = data.skills || [];
    if (list) {
      if (!skills.length) {
        list.innerHTML = '<span class="dim">Nenhuma skill carregada.</span>';
      } else {
        list.innerHTML = skills.map(function(s) {
          return '<div class="skill-card" onclick="testSkill(\'' + s.name + '\')">' +
            '<div class="skill-card-head">' +
              '<span class="skill-name">' + esc(s.name) + '</span>' +
              '<span class="skill-domain">' + esc(s.domain || "core") + '</span>' +
            '</div>' +
            '<div class="skill-desc">' + esc(s.description || "") + '</div>' +
            '<div class="skill-footer">' +
              '<div class="skill-tools-count">🛠️ ' + (s.tools ? s.tools.length : 0) + ' tools</div>' +
            '</div>' +
          '</div>';
        }).join("");
      }
    }
  } catch (err) {
    if (list) list.innerHTML = '<span class="err">Erro ao carregar skills.</span>';
  }
}

async function runMatch() {
  var input = $("match-task-input");
  var resDiv = $("match-results");
  var countBadge = $("match-count");
  if (!input || !resDiv) return;

  var task = input.value.trim();
  if (!task) return;

  resDiv.hidden = false;
  resDiv.innerHTML = '<span class="dim">Analisando...</span>';

  try {
    var data = await api("/v1/skills/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: task })
    });

    var matches = data.matches || [];
    countBadge.textContent = matches.length;

    if (!matches.length) {
      resDiv.innerHTML = '<div class="dim">Nenhuma correspondência perfeita encontrada.</div>';
    } else {
      resDiv.innerHTML = matches.map(function(m) {
        var skill = m.skill || {};
        var score = Math.round(m.score * 100) + "%";
        return '<div class="match-item">' +
          '<div class="match-item-top">' +
            '<span class="match-item-name">' + esc(skill.name) + '</span>' +
            '<span class="match-item-score">' + score + ' match</span>' +
          '</div>' +
          '<div class="match-item-tools">' +
            (skill.tools || []).map(t => '<span class="match-tool-tag">' + esc(t) + '</span>').join("") +
          '</div>' +
        '</div>';
      }).join("");
    }
  } catch (err) {
    resDiv.innerHTML = '<span class="err">Erro: ' + esc(err.message) + '</span>';
  }
}

window.testSkill = function(name) {
  $("match-task-input").value = "Teste skill: " + name;
  runMatch();
};

function handleToolSelect() {
  var name = $("tool-select").value;
  $("run-tool").disabled = !name;
  var argsEl = $("tool-args");
  argsEl.innerHTML = "";
  if (!name || !state.tools) return;
  var tool = null;
  var tools = state.tools.tools || [];
  for (var i = 0; i < tools.length; i++) {
    if (tools[i].name === name) { tool = tools[i]; break; }
  }
  var schema = tool ? (tool.input_schema || tool.inputSchema || {}) : {};
  if (!tool || !schema.properties) return;
  var props = schema.properties;
  var keys = Object.keys(props);
  for (var k = 0; k < keys.length; k++) {
    var key = keys[k];
    var label = document.createElement("label");
    label.textContent = key;
    var input = document.createElement("input");
    input.name = key;
    input.placeholder = props[key].description || key;
    label.appendChild(input);
    argsEl.appendChild(label);
  }
}

async function runTool() {
  var name = $("tool-select").value;
  if (!name) return;
  var args = {};
  var inputs = $("tool-args").querySelectorAll("input");
  for (var i = 0; i < inputs.length; i++) {
    if (inputs[i].value) args[inputs[i].name] = inputs[i].value;
  }
  var output = $("tool-output");
  output.hidden = false;
  output.textContent = "Executando...";
  try {
    var result = await api("/v1/tools/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, arguments: args }),
    });
    output.textContent = (result.result && result.result.text) || JSON.stringify(result, null, 2);
  } catch (err) {
    output.textContent = "Erro: " + err.message;
  }
}
