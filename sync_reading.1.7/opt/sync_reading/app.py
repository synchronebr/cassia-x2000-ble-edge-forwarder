#!/usr/bin/env python3
import json
import os
import time
import signal
import threading
from queue import Queue, Full, Empty

from lib.log import jlog, utc_now_iso
from lib.config import load_cfg, get_int, get_str
from lib.http_client import post_json
from lib.sse import sse_events, backoff_sleep
from lib.spool import flush_spool_streaming

from lib.ble_packet import parse_cassia_value
from lib.assembly import assembler_loop
from lib.cassia_info import fetch_cassia_info, normalize_gateway_identity

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


class Stats:
    def __init__(self):
        self._lock = threading.Lock()

        # captura SSE
        self.total_sse_events = 0
        self.invalid_json_events = 0
        self.invalid_value_events = 0
        self.reader_reconnects = 0
        self.last_sse_event_at = 0.0

        # fila de pacotes
        self.packet_enqueued = 0
        self.packet_queue_full_drops = 0
        self.max_packet_queue_depth_seen = 0

        # agrupador
        self.assemblies_open = 0
        self.assemblies_created = 0
        self.assemblies_completed = 0
        self.assemblies_timed_out = 0
        self.assemblies_evicted = 0
        self.assemblies_invalid = 0
        self.duplicate_packets = 0
        self.completed_samples = 0
        self.completed_payload_bytes = 0
        self.max_assembly_age_ms = 0

        # fila de saída
        self.outbound_enqueued = 0
        self.outbound_queue_full_drops = 0
        self.max_outbound_queue_depth_seen = 0

        # spool writer
        self.spool_written_events = 0
        self.spool_write_errors = 0
        self.spool_batches_written = 0

        # sender
        self.sent_events = 0
        self.sender_errors = 0
        self.spool_flush_calls = 0

        # identidade do gateway
        self.gateway_identity_loaded = 0
        self.gateway_mac = None
        self.gateway_ap_mac = None

        # último evento
        self.last_device = None
        self.last_ap = None
        self.last_value_len = 0

    def inc(self, field, value=1):
        with self._lock:
            setattr(self, field, getattr(self, field) + value)

    def set(self, field, value):
        with self._lock:
            setattr(self, field, value)

    def set_gateway_identity(self, gateway_mac, ap_mac):
        with self._lock:
            self.gateway_identity_loaded = 1
            self.gateway_mac = gateway_mac
            self.gateway_ap_mac = ap_mac

    def set_last_event_info(self, device, ap, value_len):
        with self._lock:
            self.last_device = device
            self.last_ap = ap
            self.last_value_len = value_len
            self.last_sse_event_at = time.time()

    def observe_packet_queue_depth(self, depth):
        with self._lock:
            if depth > self.max_packet_queue_depth_seen:
                self.max_packet_queue_depth_seen = depth

    def observe_outbound_queue_depth(self, depth):
        with self._lock:
            if depth > self.max_outbound_queue_depth_seen:
                self.max_outbound_queue_depth_seen = depth

    def observe_assembly_age_ms(self, age_ms):
        with self._lock:
            if age_ms > self.max_assembly_age_ms:
                self.max_assembly_age_ms = age_ms

    def snapshot(self):
        with self._lock:
            return {
                "total_sse_events": self.total_sse_events,
                "invalid_json_events": self.invalid_json_events,
                "invalid_value_events": self.invalid_value_events,
                "reader_reconnects": self.reader_reconnects,
                "last_sse_event_at": self.last_sse_event_at,
                "packet_enqueued": self.packet_enqueued,
                "packet_queue_full_drops": self.packet_queue_full_drops,
                "max_packet_queue_depth_seen": self.max_packet_queue_depth_seen,
                "assemblies_open": self.assemblies_open,
                "assemblies_created": self.assemblies_created,
                "assemblies_completed": self.assemblies_completed,
                "assemblies_timed_out": self.assemblies_timed_out,
                "assemblies_evicted": self.assemblies_evicted,
                "assemblies_invalid": self.assemblies_invalid,
                "duplicate_packets": self.duplicate_packets,
                "completed_samples": self.completed_samples,
                "completed_payload_bytes": self.completed_payload_bytes,
                "max_assembly_age_ms": self.max_assembly_age_ms,
                "outbound_enqueued": self.outbound_enqueued,
                "outbound_queue_full_drops": self.outbound_queue_full_drops,
                "max_outbound_queue_depth_seen": self.max_outbound_queue_depth_seen,
                "spool_written_events": self.spool_written_events,
                "spool_write_errors": self.spool_write_errors,
                "spool_batches_written": self.spool_batches_written,
                "sent_events": self.sent_events,
                "sender_errors": self.sender_errors,
                "spool_flush_calls": self.spool_flush_calls,
                "gateway_identity_loaded": self.gateway_identity_loaded,
                "gateway_mac": self.gateway_mac,
                "gateway_ap_mac": self.gateway_ap_mac,
                "last_device": self.last_device,
                "last_ap": self.last_ap,
                "last_value_len": self.last_value_len,
            }


def outbound_spool_writer_loop(
    outbound_queue,
    stats,
    spool_lock,
    pending_path,
    write_batch_size,
    write_batch_wait_ms,
    progress_every,
):
    batch = []
    last_flush = time.time()
    wait_seconds = max(0, write_batch_wait_ms) / 1000.0

    while (not STOP) or (not outbound_queue.empty()) or batch:
        item = None
        try:
            item = outbound_queue.get(timeout=0.1)
            stats.observe_outbound_queue_depth(outbound_queue.qsize())
        except Empty:
            pass

        now = time.time()

        if item is not None:
            batch.append(item)

        should_flush = False
        if batch and len(batch) >= write_batch_size:
            should_flush = True
        elif batch and (now - last_flush) >= wait_seconds:
            should_flush = True
        elif STOP and batch:
            should_flush = True

        if not should_flush:
            continue

        try:
            with spool_lock:
                with open(pending_path, "a", encoding="utf-8") as f:
                    for event in batch:
                        f.write(json.dumps(event, ensure_ascii=False) + "\n")

            stats.inc("spool_written_events", len(batch))
            stats.inc("spool_batches_written", 1)

            if progress_every > 0:
                total = stats.snapshot()["spool_written_events"]
                if total % progress_every == 0:
                    jlog(
                        SERVICE,
                        "INFO",
                        "spool_write_progress",
                        "Eventos finais persistidos em spool",
                        spool_written_events=total,
                        last_batch_size=len(batch),
                    )

            batch.clear()
            last_flush = now

        except Exception as e:
            stats.inc("spool_write_errors", 1)
            jlog(
                SERVICE,
                "WARN",
                "spool_write_error",
                "Falha ao persistir evento final em spool",
                error=str(e),
                batch_size=len(batch),
            )
            time.sleep(0.5)


def sender_loop(
    cloud_ingest_url,
    api_key,
    api_key_header,
    timeout,
    sender_batch_size,
    flush_interval_seconds,
    spool_max_bytes,
    stats,
    spool_lock,
):
    while not STOP:
        try:
            stats.inc("spool_flush_calls", 1)

            with spool_lock:
                sent = flush_spool_streaming(
                    pending_path=EVENT_PENDING,
                    sending_path=EVENT_SENDING,
                    batch_url=cloud_ingest_url,
                    post_json_fn=post_json,
                    api_key=api_key,
                    api_key_header=api_key_header,
                    timeout=timeout,
                    batch_size=sender_batch_size,
                    spool_max_bytes=spool_max_bytes,
                    jlog_fn=jlog,
                    service_name=SERVICE,
                )

            if sent:
                stats.inc("sent_events", sent)
                jlog(
                    SERVICE,
                    "INFO",
                    "spool_flushed",
                    "Eventos finais enviados para /sensors",
                    sent=sent,
                    sent_total=stats.snapshot()["sent_events"],
                )

        except Exception as e:
            stats.inc("sender_errors", 1)
            jlog(
                SERVICE,
                "WARN",
                "sender_loop_error",
                "Erro no worker de envio do spool",
                error=str(e),
            )

        slept = 0.0
        while not STOP and slept < flush_interval_seconds:
            time.sleep(0.2)
            slept += 0.2


def metrics_loop(stats, packet_queue, outbound_queue, interval_seconds):
    while not STOP:
        time.sleep(interval_seconds)
        snap = stats.snapshot()

        seconds_since_last_event = None
        if snap["last_sse_event_at"] > 0:
            seconds_since_last_event = round(time.time() - snap["last_sse_event_at"], 3)

        jlog(
            SERVICE,
            "INFO",
            "metrics",
            "Métricas periódicas",
            total_sse_events=snap["total_sse_events"],
            invalid_json_events=snap["invalid_json_events"],
            invalid_value_events=snap["invalid_value_events"],
            reader_reconnects=snap["reader_reconnects"],
            packet_enqueued=snap["packet_enqueued"],
            packet_queue_full_drops=snap["packet_queue_full_drops"],
            packet_queue_depth=packet_queue.qsize(),
            max_packet_queue_depth_seen=snap["max_packet_queue_depth_seen"],
            assemblies_open=snap["assemblies_open"],
            assemblies_created=snap["assemblies_created"],
            assemblies_completed=snap["assemblies_completed"],
            assemblies_timed_out=snap["assemblies_timed_out"],
            assemblies_evicted=snap["assemblies_evicted"],
            assemblies_invalid=snap["assemblies_invalid"],
            duplicate_packets=snap["duplicate_packets"],
            completed_samples=snap["completed_samples"],
            completed_payload_bytes=snap["completed_payload_bytes"],
            outbound_enqueued=snap["outbound_enqueued"],
            outbound_queue_full_drops=snap["outbound_queue_full_drops"],
            outbound_queue_depth=outbound_queue.qsize(),
            max_outbound_queue_depth_seen=snap["max_outbound_queue_depth_seen"],
            spool_written_events=snap["spool_written_events"],
            spool_write_errors=snap["spool_write_errors"],
            spool_batches_written=snap["spool_batches_written"],
            sent_events=snap["sent_events"],
            sender_errors=snap["sender_errors"],
            spool_flush_calls=snap["spool_flush_calls"],
            max_assembly_age_ms=snap["max_assembly_age_ms"],
            gateway_identity_loaded=snap["gateway_identity_loaded"],
            gateway_mac=snap["gateway_mac"],
            gateway_ap_mac=snap["gateway_ap_mac"],
            last_device=snap["last_device"],
            last_ap=snap["last_ap"],
            last_value_len=snap["last_value_len"],
            seconds_since_last_event=seconds_since_last_event,
        )


def main():
    ensure_dirs()

    cfg, cfg_path = load_cfg(APP_NAME, BASE_DIR, ENV_CFG)

    gateway_api = get_str(cfg, "gateway_api_base").rstrip("/")
    if not gateway_api:
        raise RuntimeError("config: gateway_api_base é obrigatório")

    gatt_path = get_str(cfg, "sse_path", "/gatt/nodes?event=1")
    gatt_url = gateway_api + gatt_path

    cloud_url = get_str(cfg, "cloud_url").rstrip("/")
    if not cloud_url:
        raise RuntimeError("config: cloud_url é obrigatório")

    cloud_ingest_url = cloud_url + get_str(cfg, "cloud_ingest_path", "/sensors")

    timeout = get_int(cfg, "timeout_seconds", 5)
    sender_batch_size = get_int(cfg, "batch_size", 20)
    flush_interval_seconds = get_int(cfg, "flush_interval_seconds", 1)
    metrics_interval_seconds = get_int(cfg, "metrics_interval_seconds", 5)

    spool_max_mb = get_int(cfg, "spool_max_mb", 20)
    spool_max_bytes = 0 if spool_max_mb <= 0 else spool_max_mb * 1024 * 1024

    api_key = get_str(cfg, "api_key", "").strip()
    api_key_header = get_str(cfg, "api_key_header", "X-API-Key").strip() or "X-API-Key"

    packet_queue_max = get_int(cfg, "packet_queue_max", 20000)
    outbound_queue_max = get_int(cfg, "outbound_queue_max", 5000)

    assembly_timeout_seconds = get_int(cfg, "assembly_timeout_seconds", 3)
    max_open_assemblies = get_int(cfg, "max_open_assemblies", 2000)

    spool_write_batch_size = get_int(cfg, "spool_write_batch_size", 100)
    spool_write_batch_wait_ms = get_int(cfg, "spool_write_batch_wait_ms", 200)

    capture_progress_every = get_int(cfg, "capture_progress_every", 1000)
    assembly_progress_every = get_int(cfg, "assembly_progress_every", 100)

    cassia_info_enabled = get_int(cfg, "cassia_info_enabled", 1)
    cassia_info_required = get_int(cfg, "cassia_info_required", 0)
    cassia_info_timeout_seconds = get_int(cfg, "cassia_info_timeout_seconds", timeout)

    if not api_key:
        jlog(
            SERVICE,
            "WARN",
            "auth_missing",
            "api_key não definida no config; enviando sem autenticação (se backend permitir)",
        )

    stats = Stats()
    packet_queue = Queue(maxsize=packet_queue_max)
    outbound_queue = Queue(maxsize=outbound_queue_max)
    spool_lock = threading.Lock()

    gateway_identity = {
        "gatewayMac": "",
        "apMac": "",
        "gatewayIp": "",
        "model": "",
        "name": "",
        "version": "",
        "raw": {},
    }

    if cassia_info_enabled:
        try:
            raw_info = fetch_cassia_info(gateway_api, timeout=cassia_info_timeout_seconds)
            gateway_identity = normalize_gateway_identity(raw_info)

            stats.set_gateway_identity(
                gateway_mac=gateway_identity.get("gatewayMac"),
                ap_mac=gateway_identity.get("apMac"),
            )

            jlog(
                SERVICE,
                "INFO",
                "cassia_info_loaded",
                "Identidade do gateway carregada no boot",
                gatewayMac=gateway_identity.get("gatewayMac"),
                apMac=gateway_identity.get("apMac"),
                gatewayIp=gateway_identity.get("gatewayIp"),
                model=gateway_identity.get("model"),
                name=gateway_identity.get("name"),
                version=gateway_identity.get("version"),
            )

        except Exception as e:
            jlog(
                SERVICE,
                "WARN",
                "cassia_info_failed",
                "Falha ao carregar identidade do gateway no boot",
                error=str(e),
                gateway_api=gateway_api,
            )

            if cassia_info_required:
                raise

    jlog(
        SERVICE,
        "INFO",
        "boot",
        "Aplicação iniciando em modo captura+agrupamento+spool+sender",
        base_dir=BASE_DIR,
        cfg_path=cfg_path,
        gatt_url=gatt_url,
        cloud_ingest_url=cloud_ingest_url,
        api_key_header=api_key_header,
        timeout_seconds=timeout,
        sender_batch_size=sender_batch_size,
        flush_interval_seconds=flush_interval_seconds,
        spool_max_mb=spool_max_mb,
        packet_queue_max=packet_queue_max,
        outbound_queue_max=outbound_queue_max,
        assembly_timeout_seconds=assembly_timeout_seconds,
        max_open_assemblies=max_open_assemblies,
        spool_write_batch_size=spool_write_batch_size,
        spool_write_batch_wait_ms=spool_write_batch_wait_ms,
        gateway_identity_loaded=stats.snapshot()["gateway_identity_loaded"],
        gateway_mac=stats.snapshot()["gateway_mac"],
        gateway_ap_mac=stats.snapshot()["gateway_ap_mac"],
    )

    assembler = threading.Thread(
        target=assembler_loop,
        args=(
            packet_queue,
            outbound_queue,
            stats,
            lambda: STOP,
            assembly_timeout_seconds,
            max_open_assemblies,
            assembly_progress_every,
            gateway_identity,
        ),
        daemon=True,
    )
    assembler.start()

    spool_writer = threading.Thread(
        target=outbound_spool_writer_loop,
        args=(
            outbound_queue,
            stats,
            spool_lock,
            EVENT_PENDING,
            spool_write_batch_size,
            spool_write_batch_wait_ms,
            assembly_progress_every,
        ),
        daemon=True,
    )
    spool_writer.start()

    sender = threading.Thread(
        target=sender_loop,
        args=(
            cloud_ingest_url,
            api_key,
            api_key_header,
            timeout,
            sender_batch_size,
            flush_interval_seconds,
            spool_max_bytes,
            stats,
            spool_lock,
        ),
        daemon=True,
    )
    sender.start()

    metrics = threading.Thread(
        target=metrics_loop,
        args=(stats, packet_queue, outbound_queue, metrics_interval_seconds),
        daemon=True,
    )
    metrics.start()

    attempt = 0

    while not STOP:
        try:
            jlog(
                SERVICE,
                "INFO",
                "gatt_connecting",
                "Conectando ao SSE GATT",
                sse_url=gatt_url,
            )
            attempt = 0

            for data_str in sse_events(gatt_url, read_timeout=60, should_stop=lambda: STOP):
                if STOP:
                    break

                if not data_str:
                    continue

                stats.inc("total_sse_events", 1)

                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    stats.inc("invalid_json_events", 1)
                    jlog(
                        SERVICE,
                        "WARN",
                        "invalid_json",
                        "Evento JSON inválido do SSE GATT",
                        error=str(e),
                        raw_data=data_str[:500],
                    )
                    continue

                value = evt.get("value", "")
                device = evt.get("device") or evt.get("id")
                ap = evt.get("ap") or "unknown"
                gateway_ap_mac = (
                    gateway_identity.get("apMac")
                    or gateway_identity.get("gatewayMac")
                    or ""
                )

                ap = (
                    evt.get("ap")
                    or evt.get("router")
                    or evt.get("gateway")
                    or evt.get("apMac")
                    or gateway_ap_mac
                )

                stats.set_last_event_info(
                    device=device,
                    ap=ap,
                    value_len=len(str(value)) if value is not None else 0,
                )

                try:
                    parsed = parse_cassia_value(value)
                except Exception as e:
                    stats.inc("invalid_value_events", 1)
                    jlog(
                        SERVICE,
                        "WARN",
                        "invalid_value",
                        "Falha ao parsear value hexadecimal do pacote BLE",
                        error=str(e),
                        device=device,
                        ap=ap,
                        value_preview=str(value)[:120],
                    )
                    continue

                now_epoch = time.time()
                now_iso = utc_now_iso()

                packet_evt = {
                    "receivedAt": now_iso,
                    "receivedAtEpoch": now_epoch,
                    "device": device,
                    "ap": ap,
                    "packet": parsed,
                    "raw_event": evt,
                }

                try:
                    packet_queue.put(packet_evt, timeout=0.2)
                    stats.inc("packet_enqueued", 1)
                    stats.observe_packet_queue_depth(packet_queue.qsize())
                except Full:
                    stats.inc("packet_queue_full_drops", 1)
                    jlog(
                        SERVICE,
                        "WARN",
                        "packet_queue_full_drop",
                        "Fila de pacotes cheia; pacote descartado",
                        queue_depth=packet_queue.qsize(),
                        device=device,
                        ap=ap,
                        sensorId=parsed.get("sensor_id"),
                        packetNo=parsed.get("packet_no"),
                        totalPackets=parsed.get("total_packets"),
                    )

                if capture_progress_every > 0:
                    snap = stats.snapshot()
                    if snap["total_sse_events"] % capture_progress_every == 0:
                        jlog(
                            SERVICE,
                            "INFO",
                            "capture_progress",
                            "Progresso de captura SSE",
                            total_sse_events=snap["total_sse_events"],
                            packet_enqueued=snap["packet_enqueued"],
                            invalid_json_events=snap["invalid_json_events"],
                            invalid_value_events=snap["invalid_value_events"],
                            packet_queue_depth=packet_queue.qsize(),
                            outbound_queue_depth=outbound_queue.qsize(),
                            spool_written_events=snap["spool_written_events"],
                            sent_events=snap["sent_events"],
                            device=snap["last_device"],
                            ap=snap["last_ap"],
                            value_len=snap["last_value_len"],
                        )

            if not STOP:
                raise RuntimeError("SSE GATT encerrou")

        except Exception as e:
            if STOP:
                break

            attempt += 1
            stats.inc("reader_reconnects", 1)

            snap = stats.snapshot()
            jlog(
                SERVICE,
                "WARN",
                "gatt_error",
                "GATT caiu/erro; reconectando",
                error=str(e),
                attempt=attempt,
                total_sse_events=snap["total_sse_events"],
                packet_enqueued=snap["packet_enqueued"],
                invalid_json_events=snap["invalid_json_events"],
                invalid_value_events=snap["invalid_value_events"],
                packet_queue_full_drops=snap["packet_queue_full_drops"],
                assemblies_open=snap["assemblies_open"],
                assemblies_completed=snap["assemblies_completed"],
                sent_events=snap["sent_events"],
                packet_queue_depth=packet_queue.qsize(),
                outbound_queue_depth=outbound_queue.qsize(),
            )
            backoff_sleep(attempt, base=1.0, cap=30.0)

    deadline = time.time() + 10.0
    while time.time() < deadline and not packet_queue.empty():
        time.sleep(0.2)

    assembler.join(timeout=3.0)
    spool_writer.join(timeout=3.0)
    sender.join(timeout=3.0)

    snap = stats.snapshot()
    jlog(
        SERVICE,
        "INFO",
        "shutdown",
        "Aplicação finalizada",
        total_sse_events=snap["total_sse_events"],
        invalid_json_events=snap["invalid_json_events"],
        invalid_value_events=snap["invalid_value_events"],
        reader_reconnects=snap["reader_reconnects"],
        packet_enqueued=snap["packet_enqueued"],
        packet_queue_full_drops=snap["packet_queue_full_drops"],
        assemblies_open=snap["assemblies_open"],
        assemblies_created=snap["assemblies_created"],
        assemblies_completed=snap["assemblies_completed"],
        assemblies_timed_out=snap["assemblies_timed_out"],
        assemblies_evicted=snap["assemblies_evicted"],
        assemblies_invalid=snap["assemblies_invalid"],
        duplicate_packets=snap["duplicate_packets"],
        completed_samples=snap["completed_samples"],
        outbound_enqueued=snap["outbound_enqueued"],
        outbound_queue_full_drops=snap["outbound_queue_full_drops"],
        spool_written_events=snap["spool_written_events"],
        spool_write_errors=snap["spool_write_errors"],
        sent_events=snap["sent_events"],
        sender_errors=snap["sender_errors"],
        packet_queue_depth=packet_queue.qsize(),
        outbound_queue_depth=outbound_queue.qsize(),
        gateway_identity_loaded=snap["gateway_identity_loaded"],
        gateway_mac=snap["gateway_mac"],
        gateway_ap_mac=snap["gateway_ap_mac"],
    )


if __name__ == "__main__":
    main()