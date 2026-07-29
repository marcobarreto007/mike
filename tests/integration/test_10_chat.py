# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Smoke tests de chat para rodar manualmente quando desejado.

Para habilitar na descoberta automatica:
    set MIKE_RUN_SLOW_INTEGRATION=1
"""

import base64
import io
import json
import os
import time
import unittest
import urllib.request

from PIL import Image


BASE = "http://127.0.0.1:8080/v1/chat/completions"


def make_png(r, g, b, w=32, h=32) -> str:
    img = Image.new("RGB", (w, h), (r, g, b))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def chat(messages, max_tokens=30, timeout=90) -> str:
    body = json.dumps({
        "model": "mike",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(BASE, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())["choices"][0]["message"]["content"].strip()


def img_msg(b64: str, text: str) -> dict:
    return {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": text},
    ]}


@unittest.skipUnless(
    os.getenv("MIKE_RUN_SLOW_INTEGRATION") == "1",
    "Smoke test manual. Defina MIKE_RUN_SLOW_INTEGRATION=1 para habilitar.",
)
class ChatSmokeTests(unittest.TestCase):
    def test_text_and_image_smoke_suite(self):
        cases = [
            ("texto simples", [{"role": "user", "content": "responde so oi"}]),
            ("imagem vermelha", [img_msg(make_png(255, 0, 0), "Que cor e esta imagem?")]),
            ("imagem azul", [img_msg(make_png(0, 0, 255), "Qual a cor?")]),
            ("matematica", [{"role": "user", "content": "quanto e 7 x 8? so o numero"}]),
            ("imagem verde", [img_msg(make_png(0, 200, 0), "De que cor e esse quadrado?")]),
        ]

        for name, messages in cases:
            with self.subTest(name=name):
                started = time.time()
                content = chat(messages)
                elapsed = time.time() - started
                self.assertTrue(content)
                self.assertLess(elapsed, 90)


if __name__ == "__main__":
    unittest.main(verbosity=2)
