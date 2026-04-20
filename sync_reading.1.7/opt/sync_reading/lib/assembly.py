import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from queue import Full, Empty

from lib.log import jlog
from lib.ble_packet import (
    HEADER_LEN,
    BLE_TX_START_MESSAGE,
    BLE_TX_TIMESTAMP,
    BLE_TX_SENSOR_IIS3DWB,
    BLE_TX_SENSOR_IIS2MDC,
    BLE_TX_SENSOR_STTS22H,
    BLE_TX_SENSOR_IMP23ABSU,
    BLE_TX_END_MESSAGE,
    SENSOR_PACKET_IDS,
    packet_type_name,
    decode_sensor_payload,
    try_parse_timestamp_payload,
)

SERVICE = "sync_reading"


def iso_utc_from_epoch(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ReadingAssembly:
    device: str
    ap: str
    started_at: float = field(default_factory=time.time)
    last_update_at: float = field(default_factory=time.time)
    first_packet_at: Optional[float] = None
    last_packet_at: Optional[float] = None

    # Todos os pacotes vistos neste frame completo
    # chave = (packet_id, packet_no)
    packets: dict = field(default_factory=dict)

    # sensor_id -> { packet_no -> payload }
    sensor_parts: dict = field(default_factory=dict)

    # sensor_id -> total_packets informado no header daquele sensor
    sensor_expected_totals: dict = field(default_factory=dict)

    start_packet_no: Optional[int] = None
    timestamp_packet_no: Optional[int] = None
    end_packet_no: Optional[int] = None

    start_payload: Optional[bytes] = None
    timestamp_payload: Optional[bytes] = None
    end_payload: Optional[bytes] = None

    bytes_received: int = 0
    last_packet_no: Optional[int] = None

    def add_part(
        self,
        packet_id,
        packet_no,
        total_packets,
        total_bytes,
        payload,
        ap,
        packet_received_at=None,
    ):
        now = packet_received_at if packet_received_at is not None else time.time()
        self.last_update_at = now
        self.ap = ap

        if self.first_packet_at is None:
            self.first_packet_at = now
        self.last_packet_at = now

        if total_packets <= 0:
            return False, "total_packets_invalid"

        if packet_no < 0:
            return False, "packet_no_invalid"

        if total_bytes < HEADER_LEN:
            return False, "total_bytes_invalid"

        pkt_key = (packet_id, packet_no)
        if pkt_key in self.packets:
            old = self.packets[pkt_key]
            if (
                old.get("total_bytes") == total_bytes
                and old.get("total_packets") == total_packets
            ):
                return True, "duplicate"
            return False, "packet_conflict"

        meta = {
            "packet_id": packet_id,
            "packet_type": packet_type_name(packet_id),
            "packet_no": packet_no,
            "total_packets": total_packets,
            "total_bytes": total_bytes,
        }

        if packet_id == BLE_TX_START_MESSAGE:
            if self.start_packet_no is not None:
                return False, "duplicate_start_packet"
            self.start_packet_no = packet_no
            self.start_payload = payload

        elif packet_id == BLE_TX_TIMESTAMP:
            if self.timestamp_packet_no is not None:
                return False, "duplicate_timestamp_packet"
            self.timestamp_packet_no = packet_no
            self.timestamp_payload = payload

        elif packet_id == BLE_TX_END_MESSAGE:
            if self.end_packet_no is not None:
                return False, "duplicate_end_packet"
            self.end_packet_no = packet_no
            self.end_payload = payload

        elif packet_id in SENSOR_PACKET_IDS:
            prev_total = self.sensor_expected_totals.get(packet_id)
            if prev_total is None:
                self.sensor_expected_totals[packet_id] = total_packets
            elif prev_total != total_packets:
                return (
                    False,
                    "sensor_total_packets_inconsistent sensor_id=%s expected=%s got=%s"
                    % (packet_id, prev_total, total_packets),
                )

            sensor_bucket = self.sensor_parts.setdefault(packet_id, {})
            if packet_no in sensor_bucket:
                old_payload = sensor_bucket[packet_no]
                if old_payload == payload:
                    return True, "duplicate"
                return False, "sensor_packet_no_conflict sensor_id=%s packet_no=%s" % (
                    packet_id,
                    packet_no,
                )

            sensor_bucket[packet_no] = payload

        else:
            return False, "unknown_packet_id=%s" % packet_id

        self.packets[pkt_key] = meta
        self.bytes_received += len(payload)
        self.last_packet_no = packet_no
        return True, "ok"

    def _packet_range_base(self, packet_numbers):
        if not packet_numbers:
            return 1
        return 0 if min(packet_numbers) == 0 else 1

    def _expected_numbers(self, total_packets, base):
        if total_packets is None or total_packets <= 0:
            return []
        if base == 0:
            return list(range(0, total_packets))
        return list(range(1, total_packets + 1))

    def is_complete(self):
        if self.start_payload is None:
            return False

        if self.timestamp_payload is None:
            return False

        if self.end_payload is None:
            return False

        if not self.sensor_parts:
            return False

        for sensor_id, bucket in self.sensor_parts.items():
            expected_total = self.sensor_expected_totals.get(sensor_id)
            if expected_total is None:
                return False

            pns = sorted(bucket.keys())
            base = self._packet_range_base(pns)
            expected = self._expected_numbers(expected_total, base)

            if len(bucket) != expected_total:
                return False

            for pn in expected:
                if pn not in bucket:
                    return False

        return True

    def missing_packets(self):
        missing = []

        if self.start_payload is None:
            missing.append({"packetType": "start", "packetId": BLE_TX_START_MESSAGE})

        if self.timestamp_payload is None:
            missing.append({"packetType": "timestamp", "packetId": BLE_TX_TIMESTAMP})

        if self.end_payload is None:
            missing.append({"packetType": "end", "packetId": BLE_TX_END_MESSAGE})

        for sensor_id in sorted(self.sensor_expected_totals.keys()):
            expected_total = self.sensor_expected_totals.get(sensor_id)
            bucket = self.sensor_parts.get(sensor_id, {})
            pns = sorted(bucket.keys())
            base = self._packet_range_base(pns)
            expected = self._expected_numbers(expected_total, base)

            for pn in expected:
                if pn not in bucket:
                    missing.append(
                        {
                            "packetType": packet_type_name(sensor_id),
                            "packetId": sensor_id,
                            "packetNo": pn,
                        }
                    )

        return missing

    def ordered_packet_numbers(self):
        return sorted(
            [(meta["packet_id"], meta["packet_no"]) for meta in self.packets.values()],
            key=lambda x: (x[0], x[1]),
        )

    def ordered_sensor_packet_numbers(self, sensor_id):
        return sorted(self.sensor_parts.get(sensor_id, {}).keys())

    def age_ms(self):
        return int((time.time() - self.started_at) * 1000.0)

    def sensor_packet_count(self):
        return sum(len(v) for v in self.sensor_parts.values())

    def total_packets_expected_for_frame(self):
        total = 0
        if self.start_payload is not None:
            total += 1
        if self.timestamp_payload is not None:
            total += 1
        if self.end_payload is not None:
            total += 1

        for sensor_id, total_packets in self.sensor_expected_totals.items():
            if sensor_id in self.sensor_parts:
                total += total_packets

        return total if total > 0 else None

    def packet_types_summary(self):
        out = []

        # start/timestamp/end
        for special_id, special_pn, special_payload in [
            (BLE_TX_START_MESSAGE, self.start_packet_no, self.start_payload),
            (BLE_TX_TIMESTAMP, self.timestamp_packet_no, self.timestamp_payload),
            (BLE_TX_END_MESSAGE, self.end_packet_no, self.end_payload),
        ]:
            if special_pn is not None:
                out.append(
                    {
                        "packetNo": special_pn,
                        "packetId": special_id,
                        "packetType": packet_type_name(special_id),
                        "payloadBytes": len(special_payload or b""),
                    }
                )

        # sensores
        for sensor_id in sorted(self.sensor_parts.keys()):
            for pn in self.ordered_sensor_packet_numbers(sensor_id):
                payload = self.sensor_parts[sensor_id][pn]
                out.append(
                    {
                        "packetNo": pn,
                        "packetId": sensor_id,
                        "packetType": packet_type_name(sensor_id),
                        "payloadBytes": len(payload or b""),
                    }
                )

        out.sort(key=lambda item: (item["packetId"], item["packetNo"]))
        return out


def assembly_time_bucket(ts_epoch, bucket_seconds=30):
    if ts_epoch is None:
        ts_epoch = time.time()
    return int(ts_epoch // bucket_seconds)


def find_matching_assembly(assemblies, device_assemblies, device, pkt, received_at_epoch):
    packet_id = pkt["packet_id"]
    packet_no = pkt["packet_no"]
    now = received_at_epoch if received_at_epoch is not None else time.time()

    candidates = []
    for key in list(device_assemblies.get(device, [])):
        asm = assemblies.get(key)
        if asm is None:
            continue

        if asm.is_complete():
            continue

        # evita reusar assembly muito antigo
        if now - asm.last_update_at > 30:
            continue

        # start sempre abre um frame novo
        if packet_id == BLE_TX_START_MESSAGE:
            continue

        # não aceitar start/timestamp/end duplicados
        if packet_id == BLE_TX_TIMESTAMP and asm.timestamp_payload is not None:
            continue
        if packet_id == BLE_TX_END_MESSAGE and asm.end_payload is not None:
            continue

        # pacote já presente
        if (packet_id, packet_no) in asm.packets:
            continue

        # start precisa casar só em assembly sem start
        if packet_id == BLE_TX_START_MESSAGE and asm.start_payload is not None:
            continue

        # timestamp e sensor só entram em assembly que já começou e ainda não fechou
        if packet_id in SENSOR_PACKET_IDS or packet_id == BLE_TX_TIMESTAMP:
            if asm.start_payload is None or asm.end_payload is not None:
                continue

        # end só entra em assembly que já começou
        if packet_id == BLE_TX_END_MESSAGE and asm.start_payload is None:
            continue

        candidates.append((key, asm))

    if not candidates:
        return None, None

    # prioridade para o assembly mais recente
    candidates.sort(key=lambda item: item[1].last_update_at, reverse=True)
    return candidates[0]


def build_new_group_key(device, pkt, received_at_epoch):
    bucket = assembly_time_bucket(received_at_epoch, bucket_seconds=30)
    packet_id = pkt.get("packet_id")
    packet_no = pkt.get("packet_no")
    return "%s:%s:%s:%s" % (device, bucket, packet_id, packet_no)


def validate_control_frames(assembly):
    if assembly.start_payload is None:
        return False, "missing_start_packet"

    if assembly.timestamp_payload is None:
        return False, "missing_timestamp_packet"

    if assembly.end_payload is None:
        return False, "missing_end_packet"

    start_ascii = assembly.start_payload.decode("utf-8", errors="ignore").strip("\x00\r\n ")
    end_ascii = assembly.end_payload.decode("utf-8", errors="ignore").strip("\x00\r\n ")

    if start_ascii and start_ascii != "START FRAME":
        return False, "invalid_start_frame_text=%s" % start_ascii

    if end_ascii and end_ascii != "END FRAME":
        return False, "invalid_end_frame_text=%s" % end_ascii

    return True, "ok"


def build_success_event(assembly):
    ts = try_parse_timestamp_payload(assembly.timestamp_payload)

    event = {
        "device": assembly.device,
        "ap": assembly.ap,

        "startSlaveReceiptAt": iso_utc_from_epoch(
            assembly.first_packet_at or assembly.started_at
        ),
        "endSlaveReceiptAt": iso_utc_from_epoch(
            assembly.last_packet_at or assembly.last_update_at
        ),
        "receivedPackets": len(assembly.packets),

        "totalPackets": assembly.total_packets_expected_for_frame(),
        "receivedAt": iso_utc_from_epoch(assembly.last_packet_at or assembly.last_update_at),
        "readingAt": iso_utc_from_epoch(
            assembly.first_packet_at or assembly.started_at
        ),
        "timestamp": ts,
    }

    if BLE_TX_SENSOR_IIS3DWB in assembly.sensor_parts:
        pns = assembly.ordered_sensor_packet_numbers(BLE_TX_SENSOR_IIS3DWB)
        payload = b"".join(assembly.sensor_parts[BLE_TX_SENSOR_IIS3DWB][pn] for pn in pns)
        event["accel"] = decode_sensor_payload(BLE_TX_SENSOR_IIS3DWB, payload)
        event["accel"]["period"] = 300

    if BLE_TX_SENSOR_IIS2MDC in assembly.sensor_parts:
        pns = assembly.ordered_sensor_packet_numbers(BLE_TX_SENSOR_IIS2MDC)
        payload = b"".join(assembly.sensor_parts[BLE_TX_SENSOR_IIS2MDC][pn] for pn in pns)
        event["mag"] = decode_sensor_payload(BLE_TX_SENSOR_IIS2MDC, payload)

    if BLE_TX_SENSOR_STTS22H in assembly.sensor_parts:
        pns = assembly.ordered_sensor_packet_numbers(BLE_TX_SENSOR_STTS22H)
        payload = b"".join(assembly.sensor_parts[BLE_TX_SENSOR_STTS22H][pn] for pn in pns)
        event["temp"] = decode_sensor_payload(BLE_TX_SENSOR_STTS22H, payload)

    if BLE_TX_SENSOR_IMP23ABSU in assembly.sensor_parts:
        pns = assembly.ordered_sensor_packet_numbers(BLE_TX_SENSOR_IMP23ABSU)
        payload = b"".join(assembly.sensor_parts[BLE_TX_SENSOR_IMP23ABSU][pn] for pn in pns)
        event["mic"] = decode_sensor_payload(BLE_TX_SENSOR_IMP23ABSU, payload)

    return event



def try_put_outbound(outbound_queue, event, stats):
    try:
        outbound_queue.put_nowait(event)
        stats.inc("outbound_enqueued", 1)
        stats.observe_outbound_queue_depth(outbound_queue.qsize())
        return True
    except Full:
        stats.inc("outbound_queue_full_drops", 1)
        jlog(
            SERVICE,
            "WARN",
            "outbound_queue_full_drop",
            "Fila de saída cheia; leitura descartada",
            device=event.get("device"),
        )
        return False


def assembler_loop(
    packet_queue,
    outbound_queue,
    stats,
    should_stop,
    assembly_timeout_seconds,
    max_open_assemblies,
    progress_every_completed,
):
    assemblies = {}
    device_assemblies = {}  # device -> set[key], for O(1) lookup
    last_housekeeping = time.monotonic()

    # Acumuladores locais: flush para stats uma vez por ciclo de housekeeping
    # (evita aquisição de lock por pacote individual)
    _acc = {
        "assemblies_created": 0,
        "assemblies_completed": 0,
        "assemblies_invalid": 0,
        "assemblies_timed_out": 0,
        "assemblies_evicted": 0,
        "duplicate_packets": 0,
        "completed_samples": 0,
        "completed_payload_bytes": 0,
    }
    _max_pq_depth = 0
    _max_age_ms = 0

    def _remove_assembly(key):
        asm = assemblies.pop(key, None)
        if asm is not None:
            keys = device_assemblies.get(asm.device)
            if keys is not None:
                keys.discard(key)
                if not keys:
                    del device_assemblies[asm.device]
        return asm

    while (not should_stop()) or (not packet_queue.empty()) or assemblies:
        item = None
        try:
            item = packet_queue.get(timeout=0.1)
            depth = packet_queue.qsize()
            if depth > _max_pq_depth:
                _max_pq_depth = depth
        except Empty:
            pass

        now = time.time()

        if item is not None:
            pkt = item["packet"]
            device = item["device"]
            ap = item["ap"]
            received_at_epoch = item.get("receivedAtEpoch")

            key, asm = find_matching_assembly(assemblies, device_assemblies, device, pkt, received_at_epoch)

            if asm is None:
                if len(assemblies) >= max_open_assemblies:
                    oldest_key = min(assemblies.keys(), key=lambda k: assemblies[k].last_update_at)
                    oldest = _remove_assembly(oldest_key)
                    _acc["assemblies_evicted"] += 1

                    jlog(
                        SERVICE,
                        "WARN",
                        "assembly_evicted",
                        "Leitura descartada por proteção de memória no edge",
                        device=oldest.device,
                        ap=oldest.ap,
                        assembly_age_ms=oldest.age_ms(),
                        bytes_received=oldest.bytes_received,
                        missing_count=len(oldest.missing_packets()),
                    )

                key = build_new_group_key(device, pkt, received_at_epoch)
                asm = ReadingAssembly(device=device, ap=ap)
                assemblies[key] = asm
                device_assemblies.setdefault(device, set()).add(key)
                _acc["assemblies_created"] += 1

            ok, reason = asm.add_part(
                packet_id=pkt["packet_id"],
                packet_no=pkt["packet_no"],
                total_packets=pkt["total_packets"],
                total_bytes=pkt["total_bytes"],
                payload=pkt["payload"],
                ap=ap,
                packet_received_at=received_at_epoch,
            )

            if reason == "duplicate":
                _acc["duplicate_packets"] += 1

            if not ok:
                _acc["assemblies_invalid"] += 1

                jlog(
                    SERVICE,
                    "WARN",
                    "assembly_invalid",
                    "Falha de consistência na leitura: %s" % reason,
                    device=device,
                    ap=ap,
                    bytes_received=asm.bytes_received,
                    missing_count=len(asm.missing_packets()),
                )

                if key in assemblies:
                    _remove_assembly(key)

            elif asm.is_complete():
                try:
                    ok_frames, frame_reason = validate_control_frames(asm)
                    if not ok_frames:
                        raise ValueError(frame_reason)

                    evt = build_success_event(asm)

                    if (
                        "accel" not in evt
                        and "mag" not in evt
                        and "temp" not in evt
                        and "mic" not in evt
                    ):
                        raise ValueError("no_sensor_payload_in_block")

                    _acc["assemblies_completed"] += 1

                    completed_samples = 0
                    if "accel" in evt:
                        completed_samples += evt["accel"].get("sampleCount", 0) or 0
                    if "mag" in evt:
                        completed_samples += evt["mag"].get("sampleCount", 0) or 0
                    if "mic" in evt:
                        completed_samples += evt["mic"].get("sampleCount", 0) or 0

                    completed_payload_bytes = 0
                    for _, sensor_bucket in asm.sensor_parts.items():
                        for _, payload in sensor_bucket.items():
                            completed_payload_bytes += len(payload)

                    _acc["completed_samples"] += completed_samples
                    _acc["completed_payload_bytes"] += completed_payload_bytes

                    age_ms = asm.age_ms()
                    if age_ms > _max_age_ms:
                        _max_age_ms = age_ms

                    try_put_outbound(outbound_queue, evt, stats)

                except Exception as e:
                    _acc["assemblies_invalid"] += 1

                    jlog(
                        SERVICE,
                        "WARN",
                        "assembly_decode_error",
                        "Erro ao reconstruir/decodificar leitura: %s" % str(e),
                        device=device,
                        ap=ap,
                        bytes_received=asm.bytes_received,
                    )

                if key in assemblies:
                    _remove_assembly(key)

        mono_now = time.monotonic()
        if mono_now - last_housekeeping >= 0.25:
            expired_keys = []

            for key, asm in assemblies.items():
                if now - asm.last_update_at >= assembly_timeout_seconds:
                    expired_keys.append(key)

            for key in expired_keys:
                asm = _remove_assembly(key)
                _acc["assemblies_timed_out"] += 1

                jlog(
                    SERVICE,
                    "WARN",
                    "assembly_timeout",
                    "Leitura incompleta descartada por timeout no edge",
                    device=asm.device,
                    ap=asm.ap,
                    assembly_age_ms=asm.age_ms(),
                    bytes_received=asm.bytes_received,
                    missing_count=len(asm.missing_packets()),
                    has_start=asm.start_payload is not None,
                    has_timestamp=asm.timestamp_payload is not None,
                    has_end=asm.end_payload is not None,
                )

            # flush acumuladores para stats com uma única aquisição de lock
            stats.inc_many(_acc)
            stats.set("assemblies_open", len(assemblies))
            stats.observe_packet_queue_depth(_max_pq_depth)
            stats.observe_assembly_age_ms(_max_age_ms)

            for k in _acc:
                _acc[k] = 0
            _max_pq_depth = 0
            _max_age_ms = 0

            last_housekeeping = mono_now

    # garante que contadores acumulados desde o último housekeeping não se percam
    stats.inc_many(_acc)
    stats.set("assemblies_open", len(assemblies))

    if assemblies:
        for _, asm in list(assemblies.items()):
            jlog(
                SERVICE,
                "WARN",
                "shutdown_incomplete",
                "Leitura incompleta descartada no encerramento do processo",
                device=asm.device,
                ap=asm.ap,
                bytes_received=asm.bytes_received,
                missing_count=len(asm.missing_packets()),
            )