/* Mike Dashboard - Email Compose */

async function handleEmail(e) {
  e.preventDefault();
  var to = $("email-to").value.trim();
  var subject = $("email-subject").value.trim();
  var body = $("email-body").value.trim();
  if (!body) return;
  var resultEl = $("email-result");
  resultEl.hidden = false;
  resultEl.textContent = "Pedindo ao Mike...";
  var emailReady = toolsHaveCapability("email");
  var calendarReady = toolsHaveCapability("calendar");
  var spreadsheetReady = toolsHaveCapability("spreadsheet") || toolsHaveCapability("excel");
  var prompt;
  if (emailReady || calendarReady || spreadsheetReady) {
    prompt = "Mike, use suas tools MCP para executar isso de verdade. " +
      (emailReady ? "Voce pode enviar email. " : "") +
      (calendarReady ? "Voce pode consultar ou mexer na agenda. " : "") +
      (spreadsheetReady ? "Voce pode ler e editar planilhas Excel/CSV. " : "") +
      "Pedido: " +
      (to ? "para " + to + ". " : "") +
      (subject ? 'assunto "' + subject + '". ' : "") +
      body;
  } else {
    prompt = "Mike, se houver alguma tool MCP de email, agenda ou planilha conectada, use-a. " +
      "Se nao houver, entregue um rascunho ou plano de acao pronto. " +
      (to ? "Para " + to + ". " : "") +
      (subject ? 'Assunto "' + subject + '". ' : "") +
      body;
  }
  try {
    var data = await api("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "mike", session_id: state.sessionId,
        private_mode: isPrivateMode(),
        messages: [{ role: "user", content: prompt }],
        max_tokens: 2048, stream: false,
      }),
    });
    var msg = data.choices && data.choices[0] && data.choices[0].message ? data.choices[0].message.content : "";
    resultEl.innerHTML = renderMd(msg || "Sem resposta");
    $("email-form").reset();
  } catch (err) {
    resultEl.textContent = "Erro: " + err.message;
  }
}
