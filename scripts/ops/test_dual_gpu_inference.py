import subprocess
import time
import urllib.request
import json
import sys

def main():
    log_path = r'c:\Users\Admin\Desktop\mike\logs\nitro_daemon.log'
    log_file = open(log_path, 'w', encoding='utf-8')
    
    cmd = [
        r'E:\F51_Nitro_20GB_DualGPU\bin\llama-server.exe',
        '-m', r'c:\Users\Admin\Desktop\mike\llm_cache\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ3_M.gguf',
        '--n-cpu-moe', '30',
        '--no-mmap',
        '-ngl', '99',
        '-c', '4096',
        '-b', '2048',
        '-ub', '512',
        '-t', '10',
        '--host', '127.0.0.1',
        '--port', '8081',
        '--no-warmup'
    ]
    
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log_file, stderr=subprocess.STDOUT)
    print(f"Server launched with PID {proc.pid}. Waiting for model load...")
    
    ready = False
    for i in range(40):
        time.sleep(2)
        try:
            res = urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).read().decode('utf-8')
            if '"status":"ok"' in res or '"ok"' in res:
                print(f"[{i*2}s] Server is READY!")
                ready = True
                break
            else:
                print(f"[{i*2}s] Server status: {res.strip()}")
        except Exception as e:
            print(f"[{i*2}s] Loading model... ({e})")
            
    if not ready:
        print("Server did not become ready in 80s.")
        sys.exit(1)
        
    # Send live completion request
    req = urllib.request.Request(
        'http://127.0.0.1:8081/v1/chat/completions',
        data=json.dumps({
            'messages': [{'role': 'user', 'content': 'Oi Mike! Voce e o Yorkshire da familia? Responde au au!'}],
            'max_tokens': 50
        }).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    
    response = urllib.request.urlopen(req, timeout=120).read().decode('utf-8')
    data = json.loads(response)
    reply = data['choices'][0]['message']['content']
    print("\n==========================================")
    print("RESPOSTA DO MODELO (F51 NITRO DUAL-GPU):")
    print(reply)
    print("==========================================\n")

if __name__ == '__main__':
    main()
