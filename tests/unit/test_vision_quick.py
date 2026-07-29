# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Smoke test rapido de visao para rodar manualmente.

Para habilitar na descoberta automatica:
    set MIKE_RUN_VISION_SMOKE=1
"""

import base64
import json
import os
import struct
import time
import unittest
import urllib.request
import zlib


def make_red_png(w=4, h=4):
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * w for _ in range(h))

    def chunk(name, data):
        crc = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@unittest.skipUnless(
    os.getenv("MIKE_RUN_VISION_SMOKE") == "1",
    "Smoke test manual. Defina MIKE_RUN_VISION_SMOKE=1 para habilitar.",
)
class VisionQuickSmokeTests(unittest.TestCase):
    def test_quick_vision_request(self):
        b64 = base64.b64encode(make_red_png()).decode()
        body = json.dumps({
            "model": "mike",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                    {"type": "text", "text": "Que cor e esta imagem? Responde em 2 palavras."},
                ],
            }],
            "max_tokens": 20,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            "http://127.0.0.1:8080/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        started = time.time()
        with urllib.request.urlopen(req, timeout=120) as response:
            payload = json.loads(response.read())
        elapsed = time.time() - started
        self.assertTrue(payload["choices"][0]["message"]["content"])
        self.assertLess(elapsed, 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
