import json
import urllib.request
import urllib.error
import sys

BASE_URL = "http://127.0.0.1:8083"

def req(path, method="GET", body=None):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

print("=== INICIANDO TESTE DE ELITE - MIKE OPERADOR ===")

# 1. Health
print("\n[1/4] Verificando Health...")
health = req("/health")
if health.get("status") == "ok":
    print(f"  > OK! Modelo: {health.get('model')}")
else:
    print(f"  > ERRO: {health}")

# 2. Teste de Ferramenta (Google Calendar)
print("\n[2/4] Testando Acesso ao Google Calendar...")
# Chamando via endpoint de tools para ver se o Mike consegue listar
tools_req = {
    "model": "mike",
    "messages": [
        {"role": "user", "content": "use a ferramenta do calendario para listar meus proximos 2 compromissos e me diga quais sao."}
    ],
    "stream": False
}
print("  > Solicitando ao Mike que consulte a agenda...")
response = req("/v1/chat/completions", method="POST", body=tools_req)

if "choices" in response:
    content = response["choices"][0]["message"]["content"]
    print(f"  > RESPOSTA DO MIKE:\n{content}")
else:
    print(f"  > ERRO NO CHAT: {response}")

# 3. Teste de Ferramenta (File System)
print("\n[3/4] Testando Escrita no Disco...")
fs_req = {
    "model": "mike",
    "messages": [
        {"role": "user", "content": "Crie um arquivo chamado 'teste_sucesso.txt' na pasta 'data/' com o texto 'PIPELINE DO MIKE 100% OPERACIONAL'. So faca isso e confirme."}
    ],
    "stream": False
}
print("  > Solicitando ao Mike que escreva no disco...")
response_fs = req("/v1/chat/completions", method="POST", body=fs_req)
if "choices" in response_fs:
    print(f"  > RESPOSTA DO MIKE: {response_fs['choices'][0]['message']['content']}")
else:
    print("  > ERRO NA ESCRITA.")

print("\n=== FIM DO TESTE ===")
