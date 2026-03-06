import json
import threading
import time
from datetime import datetime

from lib.sse import sse_lines, backoff_sleep

_RSSI_CACHE = {}
_RSSI_LOCK = threading.Lock()
_THREAD_STARTED = False


def _iso_to_ts(iso_str: str) -> float:
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def set_rssi(device_mac: str, ap_mac: str, rssi: int, observed_at_iso: str):
    if not device_mac:
        return
    ap_key = ap_mac or "unknown"
    with _RSSI_LOCK:
        dev = _RSSI_CACHE.setdefault(device_mac, {})
        dev[ap_key] = {"rssi": rssi, "lastSeen": observed_at_iso}


def get_rssi_snapshot(device_mac: str):
    with _RSSI_LOCK:
        return dict(_RSSI_CACHE.get(device_mac, {}))


def get_best_rssi(device_mac: str, max_age_sec: int = 60):
    """
    Retorna (best_ap, best_rssi, best_seen_iso) considerando somente RSSI recente.
    """
    now = time.time()
    best_ap = None
    best_obj = None

    with _RSSI_LOCK:
        aps = _RSSI_CACHE.get(device_mac, {})
        for ap, obj in aps.items():
            rssi = obj.get("rssi")
            seen = obj.get("lastSeen")
            if rssi is None or not seen:
                continue

            age = now - _iso_to_ts(seen)
            if age > max_age_sec:
                continue

            if best_obj is None or rssi > best_obj.get("rssi"):
                best_ap = ap
                best_obj = obj

    if best_obj is None:
        return None, None, None

    return best_ap, best_obj.get("rssi"), best_obj.get("lastSeen")


def _run_gap_rssi_stream(sse_url: str, should_stop, service_name: str, jlog_fn):
    attempt = 0

    while not should_stop():
        try:
            jlog_fn(service_name, "INFO", "gap_rssi_connecting", "Conectando ao SSE GAP/RSSI", sse_url=sse_url)
            attempt = 0

            for line in sse_lines(sse_url, read_timeout=60, should_stop=should_stop):
                if should_stop():
                    break

                if not line or not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()
                if not data_str:
                    continue

                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    jlog_fn(service_name, "WARN", "gap_rssi_invalid_json", "JSON inválido GAP/RSSI",
                            error=str(e), raw_data=data_str)
                    continue

                # Campos podem variar por firmware/config -> suportamos várias chaves
                device = evt.get("device") or evt.get("id") or evt.get("mac") or evt.get("node")
                ap = evt.get("ap") or evt.get("router") or evt.get("gateway") or evt.get("apMac")

                rssi = evt.get("rssi") or evt.get("RSSI")
                try:
                    rssi = int(rssi)
                except Exception:
                    continue

                observed_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
                set_rssi(device, ap, rssi, observed_at)

        except Exception as e:
            if should_stop():
                break
            attempt += 1
            jlog_fn(service_name, "WARN", "gap_rssi_error", "GAP/RSSI caiu/erro; reconectando",
                    error=str(e), attempt=attempt)
            backoff_sleep(attempt, base=1.0, cap=30.0)


def start_gap_rssi_thread(sse_url: str, should_stop, service_name: str, jlog_fn):
    """
    Inicia uma thread daemon que mantém RSSI atualizado via SSE /gap/rssi.
    Chame 1x no boot.
    """
    global _THREAD_STARTED
    if _THREAD_STARTED:
        return

    t = threading.Thread(
        target=_run_gap_rssi_stream,
        args=(sse_url, should_stop, service_name, jlog_fn),
        daemon=True
    )
    t.start()
    _THREAD_STARTED = True
    jlog_fn(service_name, "INFO", "gap_rssi_started", "Thread GAP/RSSI iniciada", sse_url=sse_url)