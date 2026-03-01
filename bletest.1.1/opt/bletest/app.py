#!/usr/bin/env python3
import json
import time
import os
import http.client
from urllib.parse import urlparse
import urllib.request
from datetime import datetime, timezone

BASE_DIR = "/opt/bletest"
LOG_DIR = os.path.join(BASE_DIR, "logs")
SPOOL_DIR = os.path.join(BASE_DIR, "spool")

APP_LOG_FILE = os.path.join(LOG_DIR, "app.log.jsonl")
EVENT_SPOOL_FILE = os.path.join(SPOOL_DIR, "events_pending.jsonl")

def ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(SPOOL_DIR, exist_ok=True)

def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def log(level, event_type, message, **meta):
    entry = {
        "ts": utc_now_iso(),
        "level": level,
        "service": "cassia-ble-forwarder",
        "event_type": event_type,
        "message": message,
        "meta": meta or {}
    }
    print(json.dumps(entry, ensure_ascii=False), flush=True)
    write_jsonl(APP_LOG_FILE, entry)

def sse_lines(url, read_timeout=60):
    p = urlparse(url)
    host = p.hostname
    port = p.port or (443 if p.scheme == "https" else 80)
    path = p.path + (("?" + p.query) if p.query else "")

    Conn = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = Conn(host, port, timeout=read_timeout)

    conn.putrequest("GET", path)
    conn.putheader("Accept", "text/event-stream")
    conn.putheader("Cache-Control", "no-cache")
    conn.endheaders()

    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f"SSE status {resp.status}")

    while True:
        line = resp.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="ignore").rstrip("\r\n")
        yield decoded

def post_json(url, payload, token="", timeout=10):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()

def post_batch(url, items, token="", timeout=10):
    payload = {
        "sentAt": utc_now_iso(),
        "items": items,
    }
    return post_json(url, payload, token=token, timeout=timeout)

def append_spool_event(event_obj):
    write_jsonl(EVENT_SPOOL_FILE, event_obj)

def read_jsonl(path):
    if not os.path.exists(path):
        return []
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                pass
    return items

def overwrite_jsonl(path, items):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    os.replace(tmp, path)

def flush_spool(cloud_url, token, timeout, batch_size):
    pending = read_jsonl(EVENT_SPOOL_FILE)
    if not pending:
        return 0

    sent_total = 0
    remaining = pending[:]

    while remaining:
        chunk = remaining[:batch_size]
        try:
            post_batch(cloud_url + "/logs/batch", chunk, token=token, timeout=timeout)
            sent_total += len(chunk)
            remaining = remaining[batch_size:]
            overwrite_jsonl(EVENT_SPOOL_FILE, remaining)
        except Exception as e:
            log("WARN", "spool_flush_failed", "Falha ao reenviar spool", error=str(e), pending=len(remaining))
            break

    return sent_total

def main():
    ensure_dirs()
    cfg = load_config("/opt/bletest/config.json")

    sse_url = cfg["gateway_api_base"].rstrip("/") + cfg.get("sse_path", "/gatt/nodes?event=1")
    cloud_url = cfg["cloud_url"]
    token = cfg.get("bearer_token", "").strip()
    timeout = int(cfg.get("timeout_seconds", 10))
    batch_size = int(cfg.get("batch_size", 50))
    flush_interval = int(cfg.get("flush_interval_seconds", 5))
    print_every = int(cfg.get("print_every", 1))

    log("INFO", "boot", "Aplicação iniciando", sse_url=sse_url, cloud_url=cloud_url)

    batch = []
    count = 0
    last_flush = time.time()

    while True:
        try:
            resent = flush_spool(cloud_url, token, timeout, batch_size)
            if resent:
                log("INFO", "spool_flushed", "Eventos do spool reenviados", resent=resent)

            log("INFO", "sse_connecting", "Conectando ao SSE", sse_url=sse_url)

            for line in sse_lines(sse_url, read_timeout=60):
                if not line:
                    continue

                if not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()

                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    log("WARN", "invalid_json", "Evento JSON inválido recebido do SSE", error=str(e))
                    continue

                count += 1
                event_obj = {
                    "ts": int(time.time()),
                    "gatewayId": cfg.get("gateway_id", "unknown-gateway"),
                    "source": "cassia-sse",
                    "event": evt
                }

                batch.append(event_obj)

                if count % print_every == 0:
                    mac = evt.get("device") or evt.get("mac") or ""
                    log("INFO", "event_received", "Evento recebido do gateway", count=count, device_mac=mac)

                now = time.time()
                should_flush = len(batch) >= batch_size or (now - last_flush) >= flush_interval

                if should_flush and batch:
                    try:
                        post_batch(cloud_url + "/sensors", batch, token=token, timeout=timeout)
                        log("INFO", "batch_sent", "Lote enviado com sucesso", batch_size=len(batch))
                        batch = []
                        last_flush = now
                    except Exception as e:
                        log("WARN", "batch_send_failed", "Falha ao enviar lote, salvando no spool", error=str(e), batch_size=len(batch))
                        for item in batch:
                            append_spool_event(item)
                        batch = []
                        last_flush = now

        except Exception as e:
            log("WARN", "sse_error", "SSE caiu/erro; reconectando", error=str(e), retry_in_seconds=2)
            time.sleep(2)

if __name__ == "__main__":
    main()