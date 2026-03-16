# /opt/sync_reading/lib/ble_packet.py

from typing import Dict, Any

HEADER_LEN = 7

# Packet IDs do protocolo
BLE_TX_START_MESSAGE = 0
BLE_TX_TIMESTAMP = 1
BLE_TX_SENSOR_IIS3DWB = 2
BLE_TX_SENSOR_IIS2MDC = 3
BLE_TX_SENSOR_STTS22H = 4
BLE_TX_SENSOR_IMP23ABSU = 5
BLE_TX_END_MESSAGE = 6

PACKET_TYPE_NAMES = {
    BLE_TX_START_MESSAGE: "start",
    BLE_TX_TIMESTAMP: "timestamp",
    BLE_TX_SENSOR_IIS3DWB: "sensor_iis3dwb",
    BLE_TX_SENSOR_IIS2MDC: "sensor_iis2mdc",
    BLE_TX_SENSOR_STTS22H: "sensor_stts22h",
    BLE_TX_SENSOR_IMP23ABSU: "sensor_imp23absu",
    BLE_TX_END_MESSAGE: "end",
}

SENSOR_PACKET_IDS = {
    BLE_TX_SENSOR_IIS3DWB,
    BLE_TX_SENSOR_IIS2MDC,
    BLE_TX_SENSOR_STTS22H,
    BLE_TX_SENSOR_IMP23ABSU,
}


def normalize_hex_string(hex_str: str) -> str:
    if hex_str is None:
        raise ValueError("value_none")

    s = str(hex_str).strip().lower()
    if not s:
        raise ValueError("value_empty")

    if s.startswith("0x"):
        s = s[2:]

    if len(s) % 2 != 0:
        raise ValueError("hex_length_must_be_even")

    return s


def packet_type_name(packet_id: int) -> str:
    return PACKET_TYPE_NAMES.get(packet_id, "unknown")


def is_sensor_packet(packet_id: int) -> bool:
    return packet_id in SENSOR_PACKET_IDS


def parse_cassia_value(hex_str: str) -> Dict[str, Any]:
    """
    Protocolo BLE:
      byte 0   : packetId
      bytes 1-2: packet_no (uint16 big-endian)
      bytes 3-4: total_packets (uint16 big-endian)
      bytes 5-6: total_bytes (uint16 big-endian, inclui header)
      bytes 7+ : payload
    """

    s = normalize_hex_string(hex_str)

    try:
        raw = bytes.fromhex(s)
    except ValueError as e:
        raise ValueError("invalid_hex_string: %s" % str(e))

    if len(raw) < HEADER_LEN:
        raise ValueError("packet_too_short len=%s" % len(raw))

    packet_id = raw[0]
    packet_no = (raw[1] << 8) | raw[2]
    total_packets = (raw[3] << 8) | raw[4]
    total_bytes = (raw[5] << 8) | raw[6]

    if total_packets <= 0:
        raise ValueError("invalid_total_packets total_packets=%s" % total_packets)

    if total_bytes < HEADER_LEN:
        raise ValueError("invalid_total_bytes total_bytes=%s" % total_bytes)

    if len(raw) < total_bytes:
        raise ValueError(
            "short_packet real_len=%s total_bytes=%s" % (len(raw), total_bytes)
        )

    packet = raw[:total_bytes]
    payload = packet[HEADER_LEN:]

    return {
        "kind": "data_packet",
        "packet_id": packet_id,
        "packet_type": packet_type_name(packet_id),
        "sensor_id": packet_id if is_sensor_packet(packet_id) else None,
        "packet_no": packet_no,
        "total_packets": total_packets,
        "total_bytes": total_bytes,
        "header_len": HEADER_LEN,
        "payload": payload,
        "payload_hex": payload.hex(),
        "raw_packet_hex": packet.hex(),
    }


def try_parse_timestamp_payload(payload_bytes: bytes) -> Dict[str, Any]:
    """
    Timestamp ainda está em formato genérico/teste.
    Então retornamos múltiplas interpretações úteis sem assumir contrato fechado.
    """
    if payload_bytes is None:
        return {
            "rawHex": None,
            "ascii": None,
            "uint32le": None,
            "uint32be": None,
            "uint64le": None,
            "uint64be": None,
        }

    out = {
        "rawHex": payload_bytes.hex(),
        "ascii": None,
        "uint32le": None,
        "uint32be": None,
        "uint64le": None,
        "uint64be": None,
    }

    try:
        txt = payload_bytes.decode("utf-8", errors="ignore")
        out["ascii"] = txt if txt else None
    except Exception:
        out["ascii"] = None

    if len(payload_bytes) == 4:
        out["uint32le"] = int.from_bytes(payload_bytes, byteorder="little", signed=False)
        out["uint32be"] = int.from_bytes(payload_bytes, byteorder="big", signed=False)

    if len(payload_bytes) == 8:
        out["uint64le"] = int.from_bytes(payload_bytes, byteorder="little", signed=False)
        out["uint64be"] = int.from_bytes(payload_bytes, byteorder="big", signed=False)

    return out


def decode_xyz_arrays(payload_bytes: bytes):
    """
    Cada amostra XYZ:
      xl, xh, yl, yh, zl, zh
    2 bytes por eixo, signed int16 little-endian.
    """
    if payload_bytes is None:
        raise ValueError("payload_none")

    if len(payload_bytes) == 0:
        return [], [], []

    if len(payload_bytes) % 6 != 0:
        raise ValueError("payload_not_multiple_of_6 len=%s" % len(payload_bytes))

    xs, ys, zs = [], [], []
    for i in range(0, len(payload_bytes), 6):
        x = int.from_bytes(payload_bytes[i:i + 2], byteorder="little", signed=True)
        y = int.from_bytes(payload_bytes[i + 2:i + 4], byteorder="little", signed=True)
        z = int.from_bytes(payload_bytes[i + 4:i + 6], byteorder="little", signed=True)

        xs.append(x)
        ys.append(y)
        zs.append(z)

    return xs, ys, zs


def decode_iis3dwb_payload(payload_bytes: bytes) -> Dict[str, Any]:
    xs, ys, zs = decode_xyz_arrays(payload_bytes)
    return {
        "sensorId": BLE_TX_SENSOR_IIS3DWB,
        "x": xs,
        "y": ys,
        "z": zs,
        "sampleCount": len(xs),
    }


def decode_iis2mdc_payload(payload_bytes: bytes) -> Dict[str, Any]:
    xs, ys, zs = decode_xyz_arrays(payload_bytes)
    return {
        "sensorId": BLE_TX_SENSOR_IIS2MDC,
        "x": xs,
        "y": ys,
        "z": zs,
        "sampleCount": len(xs),
    }


def decode_stts22h_payload(payload_bytes: bytes) -> Dict[str, Any]:
    """
    Assumimos 2 bytes little-endian signed e escala /100 para °C.
    Ajuste aqui se o firmware usar outro formato.
    """
    if payload_bytes is None or len(payload_bytes) == 0:
        raise ValueError("temp_payload_empty")

    if len(payload_bytes) < 2:
        raise ValueError("temp_payload_too_short len=%s" % len(payload_bytes))

    raw = int.from_bytes(payload_bytes[:2], byteorder="little", signed=True)
    value = raw / 100.0

    return value


def decode_imp23absu_payload(payload_bytes: bytes) -> Dict[str, Any]:
    """
    Assumimos amostras PCM de 16 bits little-endian.
    Ajuste aqui se o firmware enviar em outro formato.
    """
    if payload_bytes is None:
        raise ValueError("mic_payload_none")

    if len(payload_bytes) == 0:
        return {
            "sensorId": BLE_TX_SENSOR_IMP23ABSU,
            "samples": [],
            "sampleCount": 0,
        }

    if len(payload_bytes) % 2 != 0:
        raise ValueError("mic_payload_not_multiple_of_2 len=%s" % len(payload_bytes))

    samples = []
    for i in range(0, len(payload_bytes), 2):
        v = int.from_bytes(payload_bytes[i:i + 2], byteorder="little", signed=True)
        samples.append(v)

    return {
        "sensorId": BLE_TX_SENSOR_IMP23ABSU,
        "samples": samples,
        "sampleCount": len(samples),
    }


def decode_sensor_payload(sensor_id: int, payload_bytes: bytes) -> Dict[str, Any]:
    if sensor_id == BLE_TX_SENSOR_IIS3DWB:
        return decode_iis3dwb_payload(payload_bytes)

    if sensor_id == BLE_TX_SENSOR_IIS2MDC:
        return decode_iis2mdc_payload(payload_bytes)

    if sensor_id == BLE_TX_SENSOR_STTS22H:
        return decode_stts22h_payload(payload_bytes)

    if sensor_id == BLE_TX_SENSOR_IMP23ABSU:
        return decode_imp23absu_payload(payload_bytes)

    raise ValueError("unsupported_sensor_id=%s" % sensor_id)