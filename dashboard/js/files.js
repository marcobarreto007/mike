/* Mike Dashboard - File Browser */

async function loadFiles(path) {
  var tree = $("file-tree");
  tree.innerHTML = '<span class="dim">Carregando...</span>';
  $("file-content").hidden = true;
  try {
    var result = await api("/v1/tools/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "list_directory", arguments: { path: path || "." } }),
    });
    var text = (result.result && result.result.text) || "";
    var lines = text.split("\n").filter(Boolean);
    tree.innerHTML = lines.map(function(line) {
      var name = line.trim();
      var isDir = name.endsWith("/");
      return '<div class="fentry ' + (isDir ? "dir" : "") + '" data-path="' + esc(name) + '" data-dir="' + isDir + '">' +
        '<span class="ic">' + (isDir ? "📁" : "📄") + "</span>" +
        '<span class="nm">' + esc(name) + "</span></div>";
    }).join("") || '<span class="dim">Vazio</span>';
  } catch (err) {
    tree.innerHTML = '<span style="color:var(--danger)">' + esc(err.message) + "</span>";
  }
}

async function viewFile(path) {
  var viewer = $("file-content");
  viewer.hidden = false;
  viewer.textContent = "Carregando...";
  try {
    var result = await api("/v1/tools/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "read_text_file", arguments: { path: path } }),
    });
    viewer.textContent = (result.result && result.result.text) || "(vazio)";
  } catch (err) {
    viewer.textContent = "Erro: " + err.message;
  }
}
