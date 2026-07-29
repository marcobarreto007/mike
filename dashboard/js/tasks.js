/* Mike Dashboard - Lousa / Task Board */

var LOUSA_STORAGE_KEY = "mike_lousa_tasks";
var lousaFilter = "all";

function lousaLoad() {
  try {
    var raw = localStorage.getItem(LOUSA_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (e) { return []; }
}

function lousaSave(tasks) {
  localStorage.setItem(LOUSA_STORAGE_KEY, JSON.stringify(tasks));
}

function lousaGenId() {
  return Date.now().toString(36) + Math.random().toString(36).substr(2, 5);
}

var PRIORITY_ICONS = { "1": "🔴", "2": "🟠", "3": "🟡", "4": "🟢", "5": "⚪" };
var PRIORITY_LABELS = { "1": "Urgente", "2": "Alta", "3": "Normal", "4": "Baixa", "5": "Quando puder" };

function lousaUpdateStats(tasks) {
  var pending = 0, done = 0;
  for (var i = 0; i < tasks.length; i++) {
    if (tasks[i].done) done++; else pending++;
  }
  var elP = $("lousa-pending");
  var elD = $("lousa-done");
  var elT = $("lousa-total");
  if (elP) elP.textContent = pending + " pendente" + (pending !== 1 ? "s" : "");
  if (elD) elD.textContent = done + " concluída" + (done !== 1 ? "s" : "");
  if (elT) elT.textContent = tasks.length + " total";
  // Also update the nav badge
  if (typeof refreshTaskBadge === 'function') refreshTaskBadge();
}

function lousaFormatDate(ts) {
  if (!ts) return "";
  var d = new Date(ts);
  var dd = String(d.getDate()).padStart(2, "0");
  var mm = String(d.getMonth() + 1).padStart(2, "0");
  var hh = String(d.getHours()).padStart(2, "0");
  var mi = String(d.getMinutes()).padStart(2, "0");
  return dd + "/" + mm + " " + hh + ":" + mi;
}

function lousaChecklistProgress(checklist) {
  if (!checklist || !checklist.length) return null;
  var done = 0;
  for (var i = 0; i < checklist.length; i++) { if (checklist[i].done) done++; }
  return { done: done, total: checklist.length };
}

function lousaRenderCard(task) {
  var isDone = !!task.done;
  var priIcon = PRIORITY_ICONS[task.priority] || "🟡";
  var cls = "lousa-card" + (isDone ? " done" : "");
  var progress = lousaChecklistProgress(task.checklist);

  var html = '<div class="' + cls + '" data-task-id="' + esc(task.id) + '">';
  html += '<div class="lousa-card-top">';
  html += '<button class="lousa-check-btn' + (isDone ? ' checked' : '') + '" data-action="toggle" title="' + (isDone ? 'Reabrir' : 'Concluir') + '">';
  html += isDone ? '✅' : '⬜';
  html += '</button>';
  html += '<span class="lousa-card-pri">' + priIcon + '</span>';
  html += '<span class="lousa-card-title">' + esc(task.title) + '</span>';
  if (progress) {
    var pct = Math.round((progress.done / progress.total) * 100);
    html += '<span class="lousa-card-progress">' + progress.done + '/' + progress.total + ' (' + pct + '%)</span>';
  }
  html += '<button class="lousa-delete-btn" data-action="delete" title="Excluir">✖</button>';
  html += '</div>';

  if (task.desc) {
    html += '<div class="lousa-card-desc">' + esc(task.desc) + '</div>';
  }

  // Checklist
  if (task.checklist && task.checklist.length) {
    html += '<div class="lousa-checklist">';
    for (var i = 0; i < task.checklist.length; i++) {
      var item = task.checklist[i];
      var itemCls = "lousa-checklist-item" + (item.done ? " checked" : "");
      html += '<div class="' + itemCls + '">';
      html += '<button class="lousa-checklist-btn" data-action="check-item" data-item-idx="' + i + '">';
      html += item.done ? '☑' : '☐';
      html += '</button>';
      html += '<span class="lousa-checklist-text">' + esc(item.text) + '</span>';
      html += '<button class="lousa-checklist-remove" data-action="remove-item" data-item-idx="' + i + '" title="Remover">×</button>';
      html += '</div>';
    }
    // Add item inline
    html += '<div class="lousa-checklist-add">';
    html += '<input type="text" class="lousa-checklist-add-input" placeholder="+ Novo item..." data-action="add-item-input">';
    html += '</div>';
    html += '</div>';
  } else {
    // Option to start a checklist
    html += '<div class="lousa-checklist-add">';
    html += '<input type="text" class="lousa-checklist-add-input" placeholder="+ Adicionar checklist..." data-action="add-item-input">';
    html += '</div>';
  }

  html += '<div class="lousa-card-meta">' + lousaFormatDate(task.created);
  if (task.doneAt) html += ' • feita ' + lousaFormatDate(task.doneAt);
  html += '</div>';
  html += '</div>';
  return html;
}

function lousaRender() {
  var tasks = lousaLoad();
  lousaUpdateStats(tasks);

  // Sort: pending first (by priority), done at bottom
  tasks.sort(function(a, b) {
    if (a.done !== b.done) return a.done ? 1 : -1;
    return (parseInt(a.priority) || 3) - (parseInt(b.priority) || 3);
  });

  var container = $("lousa-tasks");
  if (!container) return;

  var filtered = tasks.filter(function(t) {
    if (lousaFilter === "pending") return !t.done;
    if (lousaFilter === "done") return !!t.done;
    return true;
  });

  if (!filtered.length) {
    var msg = lousaFilter === "done" ? "Nenhuma tarefa concluída ainda."
            : lousaFilter === "pending" ? "Todas as tarefas foram concluídas! 🎉"
            : "Nenhuma tarefa ainda. Crie uma acima! 🐾";
    container.innerHTML = '<div class="lousa-empty">' + msg + '</div>';
    return;
  }

  var html = "";
  for (var i = 0; i < filtered.length; i++) {
    html += lousaRenderCard(filtered[i]);
  }
  container.innerHTML = html;
}

function lousaAddTask(title, desc, priority, checklistItems) {
  if (!title || !title.trim()) return;
  var tasks = lousaLoad();
  var checklist = [];
  if (checklistItems && checklistItems.length) {
    for (var i = 0; i < checklistItems.length; i++) {
      var text = checklistItems[i].trim();
      if (text) checklist.push({ text: text, done: false });
    }
  }
  tasks.push({
    id: lousaGenId(),
    title: title.trim(),
    desc: (desc || "").trim(),
    priority: priority || "3",
    done: false,
    doneAt: null,
    checklist: checklist,
    created: Date.now(),
  });
  lousaSave(tasks);
  lousaRender();
}

function lousaToggleTask(taskId) {
  var tasks = lousaLoad();
  for (var i = 0; i < tasks.length; i++) {
    if (tasks[i].id === taskId) {
      tasks[i].done = !tasks[i].done;
      tasks[i].doneAt = tasks[i].done ? Date.now() : null;
      // If marking done, also check all checklist items
      if (tasks[i].done && tasks[i].checklist) {
        for (var j = 0; j < tasks[i].checklist.length; j++) {
          tasks[i].checklist[j].done = true;
        }
      }
      break;
    }
  }
  lousaSave(tasks);
  lousaRender();
}

function lousaDeleteTask(taskId) {
  var tasks = lousaLoad();
  tasks = tasks.filter(function(t) { return t.id !== taskId; });
  lousaSave(tasks);
  lousaRender();
}

function lousaToggleCheckItem(taskId, itemIdx) {
  var tasks = lousaLoad();
  for (var i = 0; i < tasks.length; i++) {
    if (tasks[i].id === taskId && tasks[i].checklist && tasks[i].checklist[itemIdx]) {
      tasks[i].checklist[itemIdx].done = !tasks[i].checklist[itemIdx].done;
      // Auto-complete task if all items checked
      var allDone = true;
      for (var j = 0; j < tasks[i].checklist.length; j++) {
        if (!tasks[i].checklist[j].done) { allDone = false; break; }
      }
      if (allDone && tasks[i].checklist.length > 0) {
        tasks[i].done = true;
        tasks[i].doneAt = Date.now();
      } else {
        tasks[i].done = false;
        tasks[i].doneAt = null;
      }
      break;
    }
  }
  lousaSave(tasks);
  lousaRender();
}

function lousaRemoveCheckItem(taskId, itemIdx) {
  var tasks = lousaLoad();
  for (var i = 0; i < tasks.length; i++) {
    if (tasks[i].id === taskId && tasks[i].checklist) {
      tasks[i].checklist.splice(itemIdx, 1);
      break;
    }
  }
  lousaSave(tasks);
  lousaRender();
}

function lousaAddCheckItem(taskId, text) {
  if (!text || !text.trim()) return;
  var tasks = lousaLoad();
  for (var i = 0; i < tasks.length; i++) {
    if (tasks[i].id === taskId) {
      if (!tasks[i].checklist) tasks[i].checklist = [];
      tasks[i].checklist.push({ text: text.trim(), done: false });
      // Unmark task as done since there's a new unchecked item
      tasks[i].done = false;
      tasks[i].doneAt = null;
      break;
    }
  }
  lousaSave(tasks);
  lousaRender();
}

function lousaClearDone() {
  var tasks = lousaLoad();
  tasks = tasks.filter(function(t) { return !t.done; });
  lousaSave(tasks);
  lousaRender();
}

// Event delegation for task actions
function lousaInitEvents() {
  var form = $("lousa-form");
  if (form) {
    form.addEventListener("submit", function(e) {
      e.preventDefault();
      var title = $("lousa-title").value;
      var desc = $("lousa-desc").value;
      var priority = $("lousa-priority").value;
      var checklistRaw = $("lousa-checklist-input") ? $("lousa-checklist-input").value : "";
      var checklistItems = checklistRaw ? checklistRaw.split(",") : [];
      lousaAddTask(title, desc, priority, checklistItems);
      form.reset();
      $("lousa-title").focus();
    });
  }

  var container = $("lousa-tasks");
  if (container) {
    container.addEventListener("click", function(e) {
      var btn = e.target.closest("[data-action]");
      if (!btn) return;
      var card = btn.closest("[data-task-id]");
      if (!card) return;
      var taskId = card.dataset.taskId;
      var action = btn.dataset.action;

      if (action === "toggle") lousaToggleTask(taskId);
      else if (action === "delete") lousaDeleteTask(taskId);
      else if (action === "check-item") lousaToggleCheckItem(taskId, parseInt(btn.dataset.itemIdx));
      else if (action === "remove-item") lousaRemoveCheckItem(taskId, parseInt(btn.dataset.itemIdx));
    });

    container.addEventListener("keydown", function(e) {
      if (e.key !== "Enter") return;
      var input = e.target.closest("[data-action='add-item-input']");
      if (!input) return;
      e.preventDefault();
      var card = input.closest("[data-task-id]");
      if (!card) return;
      lousaAddCheckItem(card.dataset.taskId, input.value);
    });
  }

  // Filters
  var filters = document.querySelectorAll(".lousa-filter");
  for (var i = 0; i < filters.length; i++) {
    filters[i].addEventListener("click", function(e) {
      lousaFilter = this.dataset.filter;
      var all = document.querySelectorAll(".lousa-filter");
      for (var j = 0; j < all.length; j++) all[j].classList.remove("active");
      this.classList.add("active");
      lousaRender();
    });
  }

  // Clear done
  var clearBtn = $("lousa-clear-done");
  if (clearBtn) {
    clearBtn.addEventListener("click", function() {
      lousaClearDone();
    });
  }

  // Initial render
  lousaRender();
}
