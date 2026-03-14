#!/usr/bin/env python3
import json
import os
import time
import signal
import struct
from datetime import datetime

from lib.log import jlog, utc_now_iso
from lib.config import load_cfg, get_int, get_str
from lib.http_client import post_batch
from lib.sse import sse_lines, backoff_sleep
from lib.spool import flush_spool_streaming
from lib.rssi import start_gap_rssi_thread, get_best_rssi

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


def iso_to_dt(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return None


def duration_ms(start_iso, end_iso):
    start_dt = iso_to_dt(start_iso)
    end_dt = iso_to_dt(end_iso)
    if not start_dt or not end_dt:
        return 0
    delta = end_dt - start_dt
    return max(0, int(delta.total_seconds() * 1000))


def normalize_hex_to_bytes(hex_str):
    if not hex_str:
        return b""

    hex_str = hex_str.strip().replace(" ", "")
    if hex_str.startswith("0x") or hex_str.startswith("0X"):
        hex_str = hex_str[2:]

    if not hex_str:
        return b""

    # Se vier ímpar, corta 1 nibble final inválido
    if len(hex_str) % 2 != 0:
        hex_str = hex_str[:-1]

    return bytes.fromhex(hex_str)


def decode_accel_readings_from_bytes(raw, byte_order="big"):
    """
    Decodifica bytes em amostras XYZ completas.

    Formato por amostra:
      xh xl yh yl zh zl   (big-endian)
    ou
      xl xh yl yh zl zh   (little-endian)

    Retorna:
      readings, remainder_bytes
    """
    if not raw:
        return [], b""

    fmt = ">h" if byte_order == "big" else "<h"

    usable = len(raw) - (len(raw) % 6)
    full = raw[:usable]
    remainder = raw[usable:]

    readings = []

    for i in range(0, len(full), 6):
        x = struct.unpack(fmt, full[i:i + 2])[0]
        y = struct.unpack(fmt, full[i + 2:i + 4])[0]
        z = struct.unpack(fmt, full[i + 4:i + 6])[0]

        readings.append({
            "x": x,
            "y": y,
            "z": z,
        })

    return readings, remainder


def add_readings_to_group(
    groups,
    evt,
    ap_final,
    best_rssi,
    best_seen,
    device_mac,
    byte_remainders,
    accel_byte_order="big"
):
    if not device_mac:
        return 0, 0

    received_at = utc_now_iso()

    group = groups.get(device_mac)
    if group is None:
        group = {
            "sentAt": received_at,
            "lastReceivedAt": received_at,
            "receiveDurationMs": 0,
            "source": "cassia-container",
            "count": 0,
            "packetCount": 0,
            "rawEventCount": 0,
            "rawByteCount": 0,
            "bufferRemainderBytes": 0,
            "rssi": best_rssi,
            "rssiObservedAt": best_seen,
            "ap": ap_final or "unknown",
            "device": device_mac,
            "readingsDetails": [],
        }
        groups[device_mac] = group
    else:
        group["lastReceivedAt"] = received_at

        if ap_final:
            group["ap"] = ap_final
        if best_rssi is not None:
            group["rssi"] = best_rssi
        if best_seen:
            group["rssiObservedAt"] = best_seen

    incoming = normalize_hex_to_bytes(evt.get("value"))
    previous_remainder = byte_remainders.get(device_mac, b"")
    combined = previous_remainder + incoming

    readings, new_remainder = decode_accel_readings_from_bytes(
        combined,
        byte_order=accel_byte_order
    )

    byte_remainders[device_mac] = new_remainder

    group["rawEventCount"] += 1
    group["rawByteCount"] += len(incoming)
    group["bufferRemainderBytes"] = len(new_remainder)
    group["packetCount"] += 1

    if readings:
        group["readingsDetails"].extend(readings)

    group["count"] = len(group["readingsDetails"])
    group["receiveDurationMs"] = duration_ms(group["sentAt"], group["lastReceivedAt"])

    jlog(
        SERVICE,
        "INFO",
        "decode_debug",
        "Diagnóstico de decodificação",
        device=device_mac,
        incoming_bytes=len(incoming),
        previous_remainder_bytes=len(previous_remainder),
        combined_bytes=len(combined),
        decoded_samples=len(readings),
        new_remainder_bytes=len(new_remainder),
    )

    return 1, len(readings)


def flush_grouped_events(
    groups,
    cloud_ingest_url,
    api_key,
    api_key_header,
    timeout,
):
    if not groups:
        return 0

    payload = []

    for _, group in groups.items():
        item = dict(group)
        item["count"] = len(item["readingsDetails"])
        item["receiveDurationMs"] = duration_ms(item["sentAt"], item["lastReceivedAt"])
        payload.append(item)

    post_batch(
        cloud_ingest_url,
        payload,
        utc_now_iso(),
        api_key=api_key,
        api_key_header=api_key_header,
        timeout=timeout
    )

    return len(payload)


def main():
    ensure_dirs()

    cfg, cfg_path = load_cfg(APP_NAME, BASE_DIR, ENV_CFG)

    gateway_api = get_str(cfg, "gateway_api_base").rstrip("/")
    if not gateway_api:
        raise RuntimeError("config: gateway_api_base é obrigatório")

    gatt_path = get_str(cfg, "sse_path", "/gatt/nodes?event=1")
    gatt_url = gateway_api + gatt_path

    gap_rssi_path = get_str(cfg, "gap_rssi_path", "/gap/rssi")
    gap_rssi_url = gateway_api + gap_rssi_path

    cloud_url = get_str(cfg, "cloud_url").rstrip("/")
    if not cloud_url:
        raise RuntimeError("config: cloud_url é obrigatório")

    cloud_ingest_url = cloud_url + get_str(cfg, "cloud_ingest_path", "/sensors")
    cloud_spool_batch_url = cloud_url + get_str(cfg, "cloud_spool_batch_path", "/logs/batch")

    timeout = get_int(cfg, "timeout_seconds", 10)
    batch_size = get_int(cfg, "batch_size", 50)
    print_every = get_int(cfg, "print_every", 50)

    spool_max_mb = get_int(cfg, "spool_max_mb", 50)
    spool_max_bytes = 0 if spool_max_mb <= 0 else spool_max_mb * 1024 * 1024

    rssi_max_age_sec = get_int(cfg, "rssi_max_age_sec", 60)
    window_seconds = get_int(cfg, "event_window_seconds", 30)

    api_key = get_str(cfg, "api_key", "").strip()
    api_key_header = get_str(cfg, "api_key_header", "X-API-Key").strip() or "X-API-Key"

    accel_byte_order = get_str(cfg, "accel_byte_order", "big").strip().lower()
    if accel_byte_order not in ("big", "little"):
        jlog(
            SERVICE,
            "WARN",
            "invalid_accel_byte_order",
            "accel_byte_order inválido; usando 'big'",
            accel_byte_order=accel_byte_order
        )
        accel_byte_order = "big"

    if not api_key:
        jlog(
            SERVICE,
            "WARN",
            "auth_missing",
            "api_key não definida no config; enviando sem autenticação (se backend permitir)"
        )

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
        api_key_header=api_key_header,
        spool_max_mb=spool_max_mb,
        rssi_max_age_sec=rssi_max_age_sec,
        event_window_seconds=window_seconds,
        accel_byte_order=accel_byte_order,
    )

    start_gap_rssi_thread(
        sse_url=gap_rssi_url,
        should_stop=lambda: STOP,
        service_name=SERVICE,
        jlog_fn=jlog,
    )

    total_packets = 0
    total_readings = 0
    attempt = 0

    grouped_events = {}
    window_started_at = None

    # MUITO IMPORTANTE:
    # guarda bytes restantes por device entre eventos SSE
    device_byte_remainders = {}

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
            jlog(
                SERVICE,
                "INFO",
                "spool_flushed",
                "Eventos do spool reenviados",
                resent=resent
            )

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

                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    jlog(
                        SERVICE,
                        "WARN",
                        "invalid_json",
                        "Evento JSON inválido do SSE GATT",
                        error=str(e),
                        raw_data=data_str
                    )
                    continue

                device_mac = evt.get("device") or evt.get("id")
                ap_evt = evt.get("ap")

                best_ap, best_rssi, best_seen = get_best_rssi(
                    device_mac,
                    max_age_sec=rssi_max_age_sec
                )
                ap_final = ap_evt or best_ap or "unknown"

                packets_added, readings_added = add_readings_to_group(
                    groups=grouped_events,
                    evt=evt,
                    ap_final=ap_final,
                    best_rssi=best_rssi,
                    best_seen=best_seen,
                    device_mac=device_mac,
                    byte_remainders=device_byte_remainders,
                    accel_byte_order=accel_byte_order,
                )

                total_packets += packets_added
                total_readings += readings_added

                if window_started_at is None and packets_added > 0:
                    window_started_at = time.time()
                    jlog(
                        SERVICE,
                        "INFO",
                        "window_started",
                        "Janela de agrupamento iniciada",
                        window_seconds=window_seconds,
                        device=device_mac
                    )

                if print_every > 0 and (total_packets % print_every == 0):
                    grp = grouped_events.get(device_mac, {})
                    jlog(
                        SERVICE,
                        "INFO",
                        "event_progress",
                        "Progresso de eventos",
                        total_packets=total_packets,
                        total_readings=total_readings,
                        grouped_devices=len(grouped_events),
                        device=device_mac,
                        ap=ap_final,
                        rssi=best_rssi,
                        group_packet_count=grp.get("packetCount"),
                        group_reading_count=grp.get("count"),
                        readings_added=readings_added,
                        receive_duration_ms=grp.get("receiveDurationMs"),
                        buffer_remainder_bytes=grp.get("bufferRemainderBytes"),
                    )

                now = time.time()
                if window_started_at is not None and (now - window_started_at) >= window_seconds:
                    try:
                        sent_groups = flush_grouped_events(
                            groups=grouped_events,
                            cloud_ingest_url=cloud_ingest_url,
                            api_key=api_key,
                            api_key_header=api_key_header,
                            timeout=timeout,
                        )

                        jlog(
                            SERVICE,
                            "INFO",
                            "window_flushed",
                            "Janela agrupada enviada",
                            window_seconds=window_seconds,
                            groups_sent=sent_groups,
                            grouped_devices=len(grouped_events),
                            total_group_readings=sum(len(g.get("readingsDetails", [])) for g in grouped_events.values()),
                            total_group_packets=sum(g.get("packetCount", 0) for g in grouped_events.values()),
                        )

                        grouped_events = {}
                        window_started_at = None

                    except Exception as e:
                        jlog(
                            SERVICE,
                            "WARN",
                            "batch_forward_failed",
                            "Falha ao encaminhar lote agrupado",
                            error=str(e),
                            grouped_devices=len(grouped_events)
                        )

            if not STOP:
                raise RuntimeError("SSE GATT encerrou")

        except Exception as e:
            if STOP:
                break

            if grouped_events:
                try:
                    sent_groups = flush_grouped_events(
                        groups=grouped_events,
                        cloud_ingest_url=cloud_ingest_url,
                        api_key=api_key,
                        api_key_header=api_key_header,
                        timeout=timeout,
                    )
                    jlog(
                        SERVICE,
                        "INFO",
                        "buffer_flushed_before_reconnect",
                        "Buffer agrupado enviado antes da reconexão",
                        groups_sent=sent_groups,
                        grouped_devices=len(grouped_events),
                        total_group_readings=sum(len(g.get("readingsDetails", [])) for g in grouped_events.values()),
                        total_group_packets=sum(g.get("packetCount", 0) for g in grouped_events.values()),
                    )
                    grouped_events = {}
                    window_started_at = None
                except Exception as flush_err:
                    jlog(
                        SERVICE,
                        "WARN",
                        "buffer_flush_before_reconnect_failed",
                        "Falha ao enviar buffer agrupado antes da reconexão",
                        error=str(flush_err),
                        grouped_devices=len(grouped_events)
                    )

            attempt += 1
            jlog(
                SERVICE,
                "WARN",
                "gatt_error",
                "GATT caiu/erro; reconectando",
                error=str(e),
                attempt=attempt
            )
            backoff_sleep(attempt, base=1.0, cap=30.0)

    if grouped_events:
        try:
            sent_groups = flush_grouped_events(
                groups=grouped_events,
                cloud_ingest_url=cloud_ingest_url,
                api_key=api_key,
                api_key_header=api_key_header,
                timeout=timeout,
            )
            jlog(
                SERVICE,
                "INFO",
                "final_buffer_flushed",
                "Buffer agrupado final enviado no shutdown",
                groups_sent=sent_groups,
                grouped_devices=len(grouped_events),
                total_group_readings=sum(len(g.get("readingsDetails", [])) for g in grouped_events.values()),
                total_group_packets=sum(g.get("packetCount", 0) for g in grouped_events.values()),
            )
        except Exception as e:
            jlog(
                SERVICE,
                "WARN",
                "final_buffer_flush_failed",
                "Falha ao enviar buffer agrupado final no shutdown",
                error=str(e),
                grouped_devices=len(grouped_events)
            )

    jlog(SERVICE, "INFO", "shutdown", "Aplicação finalizada")


if __name__ == "__main__":
    main()