#!/usr/bin/env python3
import json
import time
import http.client
from urllib.parse import urlparse
import urllib.request

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def sse_lines(url, read_timeout=60):
    p = urlparse(url)
    host = p.hostname
    port = p.port or (443 if p.scheme == "https" else 80)
    path = p.path + (("?" + p.query) if p.query else "")

    print(f"[SSE] host={host} port={port} path={path}", flush=True)

    Conn = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = Conn(host, port, timeout=read_timeout)

    conn.putrequest("GET", path)
    conn.putheader("Accept", "text/event-stream")
    conn.putheader("Cache-Control", "no-cache")
    conn.endheaders()

    print("[SSE] aguardando resposta...", flush=True)
    resp = conn.getresponse()
    print(f"[SSE] status={resp.status} reason={resp.reason}", flush=True)

    if resp.status != 200:
        raise RuntimeError(f"SSE status {resp.status}")

    print("[SSE] stream aberta", flush=True)

    while True:
        line = resp.readline()
        if not line:
            print("[SSE] stream encerrada", flush=True)
            break

        decoded = line.decode("utf-8", errors="ignore").rstrip("\r\n")
        print(f"[SSE] raw: {decoded}", flush=True)
        yield decoded

def post_json(url, payload, token="", timeout=10):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        _ = resp.read()

def main():
    print("[BOOT] app iniciando...", flush=True)
    cfg = load_config("/opt/bletest/config.json")
    print(f"[BOOT] config carregada", flush=True)

    sse_url = cfg["gateway_api_base"].rstrip("/") + cfg.get("sse_path", "/gatt/nodes?event=1")
    cloud_url = cfg["cloud_url"]
    token = cfg.get("bearer_token", "").strip()
    timeout = int(cfg.get("timeout_seconds", 10))
    print_every = int(cfg.get("print_every", 1))

    print(f"[BOOT] sse_url={sse_url}", flush=True)
    print(f"[BOOT] cloud_url={cloud_url}", flush=True)

    count = 0
    while True:
        try:
            print(f"[SSE] conectando: {sse_url}", flush=True)
            for line in sse_lines(sse_url, read_timeout=60):
                if not line:
                    continue

                if line.startswith("data:"):
                    data_str = line[len("data:"):].strip()
                    try:
                        evt = json.loads(data_str)
                    except Exception as e:
                        print(f"[WARN] json inválido: {e}", flush=True)
                        continue

                    count += 1
                    payload = {
                        "ts": int(time.time()),
                        "event": evt
                    }

                    post_json(cloud_url, payload, token=token, timeout=timeout)

                    if count % print_every == 0:
                        mac = evt.get("device") or evt.get("mac") or ""
                        print(f"[OK] encaminhado #{count} device={mac}", flush=True)

        except Exception as e:
            print(f"[WARN] SSE caiu/erro: {e} (reconectando em 2s)", flush=True)
            time.sleep(2)

if __name__ == "__main__":
    main()