#!/usr/bin/env python3
import json
import os
import time
import signal

from lib.log import jlog, utc_now_iso
from lib.config import load_cfg, get_int, get_str
from lib.http_client import post_batch
from lib.sse import sse_lines, backoff_sleep
from lib.spool import flush_spool_streaming

from lib.rssi import start_gap_rssi_thread, get_rssi_snapshot, get_best_rssi

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

    # ===== SSE URLs =====
    gatt_path = get_str(cfg, "sse_path", "/gatt/nodes?event=1")
    gatt_url = gateway_api + gatt_path

    gap_rssi_path = get_str(cfg, "gap_rssi_path", "/gap/rssi")
    gap_rssi_url = gateway_api + gap_rssi_path

    # ===== Cloud =====
    cloud_url = get_str(cfg, "cloud_url").rstrip("/")
    if not cloud_url:
        raise RuntimeError("config: cloud_url é obrigatório")

    cloud_ingest_url = cloud_url + get_str(cfg, "cloud_ingest_path", "/sensors")
    cloud_spool_batch_url = cloud_url + get_str(cfg, "cloud_spool_batch_path", "/logs/batch")

    # ===== Tunables =====
    timeout = get_int(cfg, "timeout_seconds", 10)
    batch_size = get_int(cfg, "batch_size", 50)
    flush_interval = get_int(cfg, "flush_interval_seconds", 5)
    print_every = get_int(cfg, "print_every", 50)

    spool_max_mb = get_int(cfg, "spool_max_mb", 50)
    spool_max_bytes = 0 if spool_max_mb <= 0 else spool_max_mb * 1024 * 1024

    rssi_max_age_sec = get_int(cfg, "rssi_max_age_sec", 60)

    # ===== Auth =====
    api_key = get_str(cfg, "api_key", "").strip()
    api_key_header = get_str(cfg, "api_key_header", "X-API-Key").strip() or "X-API-Key"

    if not api_key:
        jlog(SERVICE, "WARN", "auth_missing",
             "api_key não definida no config; enviando sem autenticação (se backend permitir)")

    jlog(
        SERVICE,
        "INFO",
        "boot",
        "Aplicação iniciando",
        base_dir=BASE_DIR,
        cfg_path=cfg_path,
        gatt_url=gatt_url,
        gap_rssi_url=gap_rssi_url,
        cloud_ingest_url=cloud_ingest_url,
        batch_size=batch_size,
        flush_interval=flush_interval,
        api_key_header=api_key_header,
        spool_max_mb=spool_max_mb,
        rssi_max_age_sec=rssi_max_age_sec,
    )

    # ===== Start GAP/RSSI thread =====
    start_gap_rssi_thread(
        sse_url=gap_rssi_url,
        should_stop=lambda: STOP,
        service_name=SERVICE,
        jlog_fn=jlog,
    )

    count = 0
    attempt = 0

    while not STOP:
        # Reenvio do spool (se você estiver usando esse modo no backend)
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
            jlog(SERVICE, "INFO", "gatt_connecting", "Conectando ao SSE GATT", sse_url=gatt_url)
            attempt = 0

            for line in sse_lines(gatt_url, read_timeout=60, should_stop=lambda: STOP):
                if STOP:
                    break

                if not line or not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()
                if not data_str:
                    continue

                # Parse do evento GATT
                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    jlog(SERVICE, "WARN", "invalid_json", "Evento JSON inválido do SSE GATT",
                         error=str(e), raw_data=data_str)
                    continue

                # Normalização device/ap
                device_mac = evt.get("device") or evt.get("id")
                ap_evt = evt.get("ap")

                # Enriquecimento por RSSI (último conhecido)
                signal_by_ap = get_rssi_snapshot(device_mac)
                best_ap, best_rssi, best_seen = get_best_rssi(device_mac, max_age_sec=rssi_max_age_sec)
                ap_final = ap_evt or best_ap

                payload = {
                    "receivedAt": utc_now_iso(),
                    "source": "cassia-container",
                    "ap": ap_final,
                    "device": device_mac,
                    "handle": evt.get("handle"),
                    "dataType": evt.get("dataType"),
                    "chipId": evt.get("chipId"),
                    "value": evt.get("value"),

                    # RSSI
                    "rssi": best_rssi,
                    "rssiObservedAt": best_seen,
                    "signalByAp": signal_by_ap,

                    # rastreio/debug
                    "event": evt,
                }

                # Envio imediato (1 por vez)
                try:
                    post_batch(
                        cloud_ingest_url,
                        [payload],
                        utc_now_iso(),
                        api_key=api_key,
                        api_key_header=api_key_header,
                        timeout=timeout
                    )
                except Exception as e:
                    jlog(SERVICE, "WARN", "notify_forward_failed",
                         "Falha ao encaminhar evento", error=str(e))

                count += 1
                if print_every > 0 and (count % print_every == 0):
                    jlog(SERVICE, "INFO", "event_progress", "Progresso de eventos",
                         count=count, device=device_mac, ap=ap_final, rssi=best_rssi)

            if not STOP:
                raise RuntimeError("SSE GATT encerrou")

        except Exception as e:
            if STOP:
                break
            attempt += 1
            jlog(SERVICE, "WARN", "gatt_error", "GATT caiu/erro; reconectando",
                 error=str(e), attempt=attempt)
            backoff_sleep(attempt, base=1.0, cap=30.0)

    jlog(SERVICE, "INFO", "shutdown", "Aplicação finalizada")


if __name__ == "__main__":
    main()