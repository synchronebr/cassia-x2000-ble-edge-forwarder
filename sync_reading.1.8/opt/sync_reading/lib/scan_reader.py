#!/usr/bin/env python3
"""
SSE reader para /gap/nodes — eventos de advertising do Cassia X2000.
Alimenta a scan_queue com dicts normalizados de cada sensor detectado.
"""
import json
from queue import Full

from lib.sse import sse_events, backoff_sleep
from lib.log import jlog

SERVICE = "sync_reading"


def scan_reader_loop(scan_url: str, scan_queue, stats, should_stop):
    """
    Loop de reconexão que mantém o stream /gap/nodes vivo.
    Cada evento normalizado é posto na scan_queue.

    Formato do evento colocado na fila:
      {
        "mac":      "AA:BB:CC:DD:EE:FF",   # maiúsculas
        "rssi":     -65,                    # int ou None
        "adv_data": "020106...",            # hex string
        "ap":       "...",
      }
    """
    attempt = 0

    while not should_stop():
        events_this_connection = 0
        try:
            jlog(SERVICE, "INFO", "scan_connecting", "Conectando ao SSE scan /gap/nodes", sse_url=scan_url)

            for data_str in sse_events(scan_url, read_timeout=60, should_stop=should_stop):
                if should_stop():
                    break
                if not data_str:
                    continue

                events_this_connection += 1
                stats.inc("scan_raw_events", 1)

                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    jlog(SERVICE, "WARN", "scan_invalid_json", "JSON inválido no scan SSE",
                         error=str(e), raw=data_str[:200])
                    continue

                # Log do primeiro evento de cada conexão para diagnóstico de formato
                if events_this_connection == 1:
                    jlog(SERVICE, "DEBUG", "scan_first_event",
                         "Primeiro evento SSE da conexão (diagnóstico de formato)",
                         keys=list(evt.keys()), raw=data_str[:300])

                # Cassia: formato varia entre gateway direto e Access Controller (AC)
                # Gateway direto: bdaddress / device / id / addr
                # Via AC:         bdaddrs[0].bdaddr
                mac = (
                    evt.get("bdaddress")
                    or evt.get("device")
                    or evt.get("id")
                    or evt.get("mac")
                    or evt.get("addr")
                    or ""
                )

                # Formato AC: bdaddrs é uma lista de objetos com "bdaddr"
                if not mac:
                    bdaddrs = evt.get("bdaddrs")
                    if bdaddrs and isinstance(bdaddrs, list) and bdaddrs[0]:
                        mac = bdaddrs[0].get("bdaddr") or bdaddrs[0].get("bdad") or ""

                if not mac:
                    stats.inc("scan_no_mac_drops", 1)
                    continue

                raw_rssi = evt.get("rssi") or evt.get("RSSI")
                rssi = None
                if raw_rssi is not None:
                    try:
                        rssi = int(raw_rssi)
                    except Exception:
                        pass

                adv_data = (
                    evt.get("adv_data")
                    or evt.get("advData")
                    or evt.get("adData")
                    or evt.get("adv")
                    or evt.get("data")
                    or ""
                )

                ap = evt.get("ap") or ""

                addr_type = "random"
                bdaddrs = evt.get("bdaddrs")
                if bdaddrs and isinstance(bdaddrs, list) and bdaddrs[0]:
                    raw_type = bdaddrs[0].get("bdaddrType") or ""
                    if raw_type.lower() == "public":
                        addr_type = "public"

                scan_event = {
                    "mac": mac.upper(),
                    "rssi": rssi,
                    "adv_data": adv_data,
                    "ap": ap,
                    "addr_type": addr_type,
                }

                try:
                    scan_queue.put_nowait(scan_event)
                    stats.inc("scan_events_received", 1)
                except Full:
                    stats.inc("scan_queue_full_drops", 1)

            if not should_stop():
                if events_this_connection > 0:
                    attempt = 0
                raise RuntimeError("SSE scan encerrou")

        except Exception as e:
            if should_stop():
                break
            attempt += 1
            jlog(SERVICE, "WARN", "scan_error", "Scan SSE caiu; reconectando",
                 error=str(e), attempt=attempt)
            backoff_sleep(attempt, base=1.0, cap=30.0)
