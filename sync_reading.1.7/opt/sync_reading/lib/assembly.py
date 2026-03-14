import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from queue import Full, Empty

from lib.log import jlog
from lib.ble_packet import decode_xyz_arrays

SERVICE = "sync_reading"


def iso_utc_from_epoch(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ReadingAssembly:
    device: str
    ap: str
    sensor_id: int
    total_packets: Optional[int] = None
    packet_index_base: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    last_update_at: float = field(default_factory=time.time)
    first_packet_at: Optional[float] = None
    last_packet_at: Optional[float] = None
    parts: dict = field(default_factory=dict)
    bytes_received: int = 0
    last_packet_no: Optional[int] = None

    def add_part(self, packet_no, total_packets, total_bytes, payload, ap, packet_received_at=None):
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

        if total_bytes < 6:
            return False, "total_bytes_invalid"

        if self.total_packets is None:
            self.total_packets = total_packets
        elif self.total_packets != total_packets:
            return False, (
                "total_packets_inconsistent expected=%s got=%s"
                % (self.total_packets, total_packets)
            )

        if self.packet_index_base is None:
            if packet_no == 0:
                self.packet_index_base = 0
            elif packet_no == 1:
                self.packet_index_base = 1

        if packet_no in self.parts:
            return True, "duplicate"

        self.parts[packet_no] = payload
        self.bytes_received += len(payload)
        self.last_packet_no = packet_no
        return True, "ok"

    def is_complete(self):
        if self.total_packets is None:
            return False

        base = 1 if self.packet_index_base is None else self.packet_index_base
        expected = range(0, self.total_packets) if base == 0 else range(1, self.total_packets + 1)

        if len(self.parts) != self.total_packets:
            return False

        for pn in expected:
            if pn not in self.parts:
                return False

        return True

    def missing_packets(self):
        if self.total_packets is None:
            return []

        base = 1 if self.packet_index_base is None else self.packet_index_base
        expected = range(0, self.total_packets) if base == 0 else range(1, self.total_packets + 1)
        return [pn for pn in expected if pn not in self.parts]

    def ordered_packet_numbers(self):
        return sorted(self.parts.keys())

    def age_ms(self):
        return int((time.time() - self.started_at) * 1000.0)


def build_group_key(device, sensor_id):
    return "%s:%s" % (device, sensor_id)

def build_success_event(assembly, payload_bytes):
    xs, ys, zs = decode_xyz_arrays(payload_bytes)
    pns = assembly.ordered_packet_numbers()

    return {
        "accel": {
            "x": xs,
            "y": ys,
            "z": zs,
        },
        "ap": assembly.ap,
        "device": assembly.device,
        "sensorId": assembly.sensor_id,
        "endSlaveReceiptAt": iso_utc_from_epoch(assembly.last_packet_at or assembly.last_update_at),
        "packetEnd": pns[-1] if pns else None,
        "packetIndexBase": assembly.packet_index_base if assembly.packet_index_base is not None else 1,
        "packetStart": pns[0] if pns else None,
        "payloadBytes": len(payload_bytes),
        "receivedAt": iso_utc_from_epoch(assembly.last_packet_at or assembly.last_update_at),
        "receivedPackets": len(assembly.parts),
        "sampleCount": len(xs),
        "startSlaveReceiptAt": iso_utc_from_epoch(assembly.first_packet_at or assembly.started_at),
    }


def build_error_event(assembly, error_code, error_message, missing_packets=None, gateway_identity=None):
    if missing_packets is None:
        missing_packets = []

    return {
        "ap": assembly.ap,
        "device": assembly.device,
        "endSlaveReceiptAt": iso_utc_from_epoch(assembly.last_packet_at or assembly.last_update_at),
        "errorCode": error_code,
        "errorMessage": error_message,
        "payloadBytesReceived": assembly.bytes_received,
        "receivedPackets": len(assembly.parts),
        "startSlaveReceiptAt": iso_utc_from_epoch(assembly.first_packet_at or assembly.started_at),
        "totalPacketsExpected": assembly.total_packets,
        "missingPackets": missing_packets,
        "assemblyAgeMs": assembly.age_ms(),
    }


def try_put_outbound(outbound_queue, event, stats):
    try:
        outbound_queue.put(event, timeout=0.5)
        stats.inc("outbound_enqueued", 1)
        stats.observe_outbound_queue_depth(outbound_queue.qsize())
        return True
    except Full:
        stats.inc("outbound_queue_full_drops", 1)
        jlog(
            SERVICE,
            "WARN",
            "outbound_queue_full_drop",
            "Fila de saída cheia; evento final descartado",
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
    gateway_identity=None,
):
    assemblies = {}
    last_housekeeping = time.monotonic()

    while (not should_stop()) or (not packet_queue.empty()) or assemblies:
        item = None
        try:
            item = packet_queue.get(timeout=0.1)
            stats.observe_packet_queue_depth(packet_queue.qsize())
        except Empty:
            pass

        now = time.time()

        if item is not None:
            pkt = item["packet"]
            device = item["device"]
            ap = item["ap"]
            sensor_id = pkt["sensor_id"]
            key = build_group_key(device, sensor_id)

            asm = assemblies.get(key)

            if asm is None:
                if len(assemblies) >= max_open_assemblies:
                    oldest_key = min(assemblies.keys(), key=lambda k: assemblies[k].last_update_at)
                    oldest = assemblies.pop(oldest_key)
                    stats.inc("assemblies_evicted", 1)
                    stats.set("assemblies_open", len(assemblies))

                    evt = build_error_event(
                        oldest,
                        "assembly_evicted",
                        "Leitura descartada por proteção de memória no edge",
                        oldest.missing_packets(),
                        gateway_identity,
                    )
                    try_put_outbound(outbound_queue, evt, stats)

                asm = ReadingAssembly(
                    device=device,
                    ap=ap,
                    sensor_id=sensor_id,
                )
                assemblies[key] = asm
                stats.inc("assemblies_created", 1)
                stats.set("assemblies_open", len(assemblies))

            ok, reason = asm.add_part(
                packet_no=pkt["packet_no"],
                total_packets=pkt["total_packets"],
                total_bytes=pkt["total_bytes"],
                payload=pkt["payload"],
                ap=ap,
                packet_received_at=item.get("receivedAtEpoch"),
            )

            if reason == "duplicate":
                stats.inc("duplicate_packets", 1)

            if not ok:
                stats.inc("assemblies_invalid", 1)

                evt = build_error_event(
                    asm,
                    "assembly_invalid",
                    "Falha de consistência na leitura: %s" % reason,
                    asm.missing_packets(),
                    gateway_identity,
                )
                try_put_outbound(outbound_queue, evt, stats)

                if key in assemblies:
                    del assemblies[key]
                    stats.set("assemblies_open", len(assemblies))
                continue

            if asm.is_complete():
                try:
                    pns = asm.ordered_packet_numbers()
                    payload_bytes = b"".join(asm.parts[pn] for pn in pns)
                    evt = build_success_event(asm, payload_bytes)

                    stats.inc("assemblies_completed", 1)
                    stats.inc("completed_samples", evt["sampleCount"])
                    stats.inc("completed_payload_bytes", evt["payloadBytes"])
                    stats.observe_assembly_age_ms(asm.age_ms())

                    try_put_outbound(outbound_queue, evt, stats)

                except Exception as e:
                    stats.inc("assemblies_invalid", 1)

                    evt = build_error_event(
                        asm,
                        "decode_error",
                        "Erro ao reconstruir/decodificar leitura: %s" % str(e),
                        [],
                        gateway_identity,
                    )
                    try_put_outbound(outbound_queue, evt, stats)

                if key in assemblies:
                    del assemblies[key]
                    stats.set("assemblies_open", len(assemblies))

        mono_now = time.monotonic()
        if mono_now - last_housekeeping >= 0.25:
            expired_keys = []

            for key, asm in assemblies.items():
                if now - asm.last_update_at >= assembly_timeout_seconds:
                    expired_keys.append(key)

            for key in expired_keys:
                asm = assemblies.pop(key)
                stats.inc("assemblies_timed_out", 1)
                stats.set("assemblies_open", len(assemblies))

                evt = build_error_event(
                    asm,
                    "assembly_timeout",
                    "Leitura incompleta descartada por timeout no edge",
                    asm.missing_packets(),
                    gateway_identity,
                )
                try_put_outbound(outbound_queue, evt, stats)

            last_housekeeping = mono_now

    if assemblies:
        for _, asm in list(assemblies.items()):
            evt = build_error_event(
                asm,
                "shutdown_incomplete",
                "Leitura incompleta descartada no encerramento do processo",
                asm.missing_packets(),
                gateway_identity,
            )
            try_put_outbound(outbound_queue, evt, stats)