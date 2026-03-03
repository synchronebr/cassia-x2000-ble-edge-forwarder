#!/usr/bin/env python3
import json
import os
import time
import signal

from lib.log import jlog, utc_now_iso
from lib.config import load_cfg, get_int, get_str
from lib.http_client import post_batch
from lib.sse import sse_lines, backoff_sleep
from lib.spool import append_jsonl, flush_spool_streaming, spool_size_bytes

APP_NAME = "sync_reading"
SERVICE = "sync_reading"

BASE_DIR = os.environ.get("APP_BASE_DIR", "/opt/sync_reading")
ENV_CFG = os.environ.get("APP_CONFIG")

LOG_DIR = os.path.join(BASE_DIR, "logs")
SPOOL_DIR = os.path.join(BASE_DIR, "spool")
EVENT_PENDING = os.path.join(SPOOL_DIR, "events_pending.jsonl")
EVENT_SENDING = os.path.join(SPOOL_DIR, "events_pending.jsonl.sending")

STOP = False

def handle_stop(*_):
    global STOP
    STOP = True

signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)

def ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(SPOOL_DIR, exist_ok=True)

def main():
    ensure_dirs()

    cfg, cfg_path = load_cfg(APP_NAME, BASE_DIR, ENV_CFG)

    gateway_api = get_str(cfg, "gateway_api_base").rstrip("/")
    if not gateway_api:
        raise RuntimeError("config: gateway_api_base é obrigatório")

    sse_path = get_str(cfg, "sse_path", "/gatt/nodes?event=1")
    sse_url = gateway_api + sse_path

    cloud_url = get_str(cfg, "cloud_url").rstrip("/")
    if not cloud_url:
        raise RuntimeError("config: cloud_url é obrigatório")

    cloud_ingest_url = cloud_url + get_str(cfg, "cloud_ingest_path", "/sensors")
    cloud_spool_batch_url = cloud_url + get_str(cfg, "cloud_spool_batch_path", "/logs/batch")

    timeout = get_int(cfg, "timeout_seconds", 10)
    batch_size = get_int(cfg, "batch_size", 50)
    flush_interval = get_int(cfg, "flush_interval_seconds", 5)
    print_every = get_int(cfg, "print_every", 50)

    api_key = get_str(cfg, "api_key", "").strip()
    api_key_header = get_str(cfg, "api_key_header", "X-API-Key").strip() or "X-API-Key"

    spool_max_mb = get_int(cfg, "spool_max_mb", 50)
    spool_max_bytes = 0 if spool_max_mb <= 0 else spool_max_mb * 1024 * 1024

    if not api_key:
        jlog(SERVICE, "WARN", "auth_missing", "api_key não definida no config; enviando sem autenticação (se backend permitir)")

    # Não logar api_key!
    jlog(SERVICE, "INFO", "boot", "Aplicação iniciando",
         base_dir=BASE_DIR, cfg_path=cfg_path, sse_url=sse_url,
         cloud_ingest_url=cloud_ingest_url, batch_size=batch_size,
         flush_interval=flush_interval, api_key_header=api_key_header,
         spool_max_mb=spool_max_mb)

    batch = []
    count = 0
    last_flush = time.monotonic()
    attempt = 0

    while not STOP:
        resent = flush_spool_streaming(
            pending_path=EVENT_PENDING,
            sending_path=EVENT_SENDING,
            batch_url=cloud_spool_batch_url,
            post_batch_fn=post_batch,
            api_key=api_key,
            api_key_header=api_key_header,
            timeout=timeout,
            batch_size=batch_size,
            sent_at_iso_fn=utc_now_iso,
            spool_max_bytes=spool_max_bytes,
            jlog_fn=jlog,
            service_name=SERVICE,
        )
        if resent:
            jlog(SERVICE, "INFO", "spool_flushed", "Eventos do spool reenviados", resent=resent)

        try:
            jlog(SERVICE, "INFO", "sse_connecting", "Conectando ao SSE", sse_url=sse_url)
            attempt = 0

            for line in sse_lines(sse_url, read_timeout=60, should_stop=lambda: STOP):
                if STOP:
                    break
                if not line or not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()
                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    jlog(SERVICE, "WARN", "invalid_json", "Evento JSON inválido do SSE", error=str(e))
                    continue

                count += 1
                event_obj = {
                    "ts": int(time.time()),
                    "source": "cassia-sse",
                    "event": evt,
                }
                batch.append(event_obj)

                if print_every > 0 and (count % print_every == 0):
                    mac = evt.get("device") or evt.get("mac") or ""
                    jlog(SERVICE, "INFO", "event_progress", "Progresso de eventos", count=count, device_mac=mac)

                now = time.monotonic()
                should_flush = (len(batch) >= batch_size) or ((now - last_flush) >= flush_interval)

                if should_flush and batch:
                    try:
                        post_batch(cloud_ingest_url, batch, utc_now_iso(), api_key=api_key, api_key_header=api_key_header, timeout=timeout)
                        jlog(SERVICE, "INFO", "batch_sent", "Lote enviado com sucesso", batch_size=len(batch))
                        batch[:] = []
                        last_flush = now
                    except Exception as e:
                        jlog(SERVICE, "WARN", "batch_send_failed", "Falha ao enviar lote; salvando no spool",
                             error=str(e), batch_size=len(batch))

                        if spool_max_bytes > 0 and spool_size_bytes(EVENT_PENDING) >= spool_max_bytes:
                            jlog(SERVICE, "WARN", "spool_limit_reached",
                                 "Spool atingiu limite; descartando lote para proteger storage",
                                 spool_max_bytes=spool_max_bytes, dropped=len(batch))
                        else:
                            for item in batch:
                                append_jsonl(EVENT_PENDING, item)

                        batch[:] = []
                        last_flush = now

            if not STOP:
                raise RuntimeError("SSE stream encerrou")

        except Exception as e:
            if STOP:
                break
            attempt += 1
            jlog(SERVICE, "WARN", "sse_error", "SSE caiu/erro; reconectando", error=str(e), attempt=attempt)
            backoff_sleep(attempt, base=1.0, cap=30.0)

    if batch:
        jlog(SERVICE, "INFO", "shutdown_spooling", "Encerrando; salvando lote pendente no spool", batch_size=len(batch))
        if spool_max_bytes > 0 and spool_size_bytes(EVENT_PENDING) >= spool_max_bytes:
            jlog(SERVICE, "WARN", "spool_limit_reached",
                 "Spool atingiu limite no shutdown; descartando lote para proteger storage",
                 spool_max_bytes=spool_max_bytes, dropped=len(batch))
        else:
            for item in batch:
                append_jsonl(EVENT_PENDING, item)
        batch[:] = []

    jlog(SERVICE, "INFO", "shutdown", "Aplicação finalizada")

if __name__ == "__main__":
    main()
