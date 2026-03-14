# /opt/sync_reading/lib/ble_packet.py

def parse_cassia_value(hex_str: str):
    """
    Header assumido:
      byte 0: sensor_id
      byte 1: packet_no high
      byte 2: packet_no low
      byte 3: total_packets high
      byte 4: total_packets low
      byte 5: total_bytes_in_packet (inclui header)

    Exemplo:
      01 00 CE 00 CE 60 ...
    """

    if not hex_str:
        raise ValueError("value_empty")

    s = str(hex_str).strip().lower()
    if s.startswith("0x"):
        s = s[2:]

    raw = bytes.fromhex(s)

    if len(raw) < 6:
        raise ValueError(f"packet_too_short len={len(raw)}")

    sensor_id = raw[0]
    packet_no = (raw[1] << 8) | raw[2]
    total_packets = (raw[3] << 8) | raw[4]
    total_bytes = raw[5]

    if total_packets <= 0:
        raise ValueError(f"invalid_total_packets total_packets={total_packets}")

    if total_bytes < 6:
        raise ValueError(f"invalid_total_bytes total_bytes={total_bytes}")

    if len(raw) < total_bytes:
        raise ValueError(
            f"short_packet real_len={len(raw)} total_bytes={total_bytes}"
        )

    packet = raw[:total_bytes]
    payload = packet[6:]

    return {
        "sensor_id": sensor_id,
        "packet_no": packet_no,
        "total_packets": total_packets,
        "total_bytes": total_bytes,
        "payload": payload,
        "raw_packet_hex": packet.hex(),
    }


def decode_xyz_arrays(payload_bytes: bytes):
    """
    Cada amostra:
      xl, xh, yl, yh, zl, zh
    2 bytes por eixo, signed int16 little-endian.
    """
    if len(payload_bytes) % 6 != 0:
        raise ValueError(f"payload_not_multiple_of_6 len={len(payload_bytes)}")

    xs, ys, zs = [], [], []
    for i in range(0, len(payload_bytes), 6):
        x = int.from_bytes(payload_bytes[i:i+2], byteorder="little", signed=True)
        y = int.from_bytes(payload_bytes[i+2:i+4], byteorder="little", signed=True)
        z = int.from_bytes(payload_bytes[i+4:i+6], byteorder="little", signed=True)

        xs.append(x)
        ys.append(y)
        zs.append(z)

    return xs, ys, zs