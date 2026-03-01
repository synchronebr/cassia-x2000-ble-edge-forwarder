#!/usr/bin/env python3
import json
import time
import http.client
from urllib.parse import urlparse
import urllib.request
from datetime import datetime, timezone


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def sse_lines(url, read_timeout=60):
    p = urlparse(url)
    host = p.hostname
    port = p.port or (443 if p.scheme == "https" else 80)
    path = p.path + (("?" + p.query) if p.query else "")

    print(f"[SSE] host={host} port={port} path={path}", flush=True)

    Conn = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = Conn(host, port, timeout=read_timeout)

    conn.putrequest("GET", path)
    conn.putheader("Accept", "text/event-stream")
    conn.putheader("Cache-Control", "no-cache")
    conn.endheaders()

    print("[SSE] aguardando resposta...", flush=True)
    resp = conn.getresponse()
    print(f"[SSE] status={resp.status} reason={resp.reason}", flush=True)

    if resp.status != 200:
        raise RuntimeError(f"SSE status {resp.status}")

    print("[SSE] stream aberta", flush=True)

    while True:
        line = resp.readline()
        if not line:
            print("[SSE] stream encerrada", flush=True)
            break

        decoded = line.decode("utf-8", errors="ignore").rstrip("\r\n")
        yield decoded


def post_json(url, payload, token="", timeout=10):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        _ = resp.read()


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_hex_string(s):
    if not s or len(s) % 2 != 0:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def int16_le(b0, b1):
    value = b0 | (b1 << 8)
    if value >= 0x8000:
        value -= 0x10000
    return value


def parse_frame(hex_value):
    raw = bytes.fromhex(hex_value)

    if len(raw) < 4:
        raise ValueError("frame curto demais para header")

    header = raw[:4]
    body = raw[4:]

    byte1 = header[0]
    byte2 = header[1]  # sensor
    byte3 = header[2]  # numero do pacote
    byte4 = header[3]  # total de pacotes

    samples = []
    offset = 0

    while offset + 6 <= len(body):
        x = int16_le(body[offset], body[offset + 1])
        y = int16_le(body[offset + 2], body[offset + 3])
        z = int16_le(body[offset + 4], body[offset + 5])

        samples.append({
            "xRaw": x,
            "yRaw": y,
            "zRaw": z,
        })
        offset += 6

    trailing = body[offset:] if offset < len(body) else b""

    return {
        "header": {
            "byte1": byte1,
            "byte2": byte2,
            "byte3": byte3,
            "byte4": byte4,
            "hex": hex_value[:8].upper(),
        },
        "sensor": byte2,
        "packetNumber": byte3,
        "totalPackets": byte4,
        "samples": samples,
        "sampleCount": len(samples),
        "trailingHex": trailing.hex().upper() if trailing else "",
        "frameHex": hex_value.upper(),
    }


def normalize_sensor_name(sensor_id):
    if sensor_id == 0:
        return "sensor0_vibration"
    return f"sensor{sensor_id}"


def get_gateway_mac(evt):
    return evt.get("ap") or evt.get("gatewayMac") or ""


def get_device_mac(evt):
    # no seu rawEvent real veio "id"
    return evt.get("device") or evt.get("mac") or evt.get("id") or ""


def make_group_key(gateway_mac, device_mac, sensor_id):
    return f"{gateway_mac}|{device_mac}|{sensor_id}"


def build_consolidated_payload(group):
    """
    Monta 1 body só com todos os pacotes do grupo.
    """
    ordered_packet_numbers = sorted(group["packets"].keys())

    all_samples = []
    packets_meta = []

    for pkt_num in ordered_packet_numbers:
        frame = group["packets"][pkt_num]
        all_samples.extend(frame["samples"])
        packets_meta.append({
            "packetNumber": pkt_num,
            "sampleCount": frame["sampleCount"],
            "header": frame["header"],
        })

    return {
        "gatewayMac": group["gatewayMac"],
        "deviceMac": group["deviceMac"],
        "readingAt": now_iso(),
        "sensor": {
            "id": group["sensorId"],
            "name": normalize_sensor_name(group["sensorId"]),
        },
        "eventType": group["eventType"],
        "handle": group["handle"],
        "packetCount": len(ordered_packet_numbers),
        "expectedPackets": group["expectedPackets"],
        "sampleCount": len(all_samples),
        "readings": all_samples,
        "temperature": [],
        "packets": packets_meta,
    }


def create_group(evt, frame):
    return {
        "gatewayMac": get_gateway_mac(evt),
        "deviceMac": get_device_mac(evt),
        "sensorId": frame["sensor"],
        "expectedPackets": frame["totalPackets"],
        "eventType": evt.get("dataType"),
        "handle": evt.get("handle"),
        "createdAt": time.time(),
        "lastUpdateAt": time.time(),
        "packets": {}
    }


def main():
    print("[BOOT] app iniciando...", flush=True)
    cfg = load_config("/opt/bletest/config.json")
    print("[BOOT] config carregada", flush=True)

    sse_url = cfg["gateway_api_base"].rstrip("/") + cfg.get("sse_path", "/gatt/nodes?event=1")
    cloud_url = cfg["cloud_url"]
    token = cfg.get("bearer_token", "").strip()
    timeout = int(cfg.get("timeout_seconds", 10))
    print_every = int(cfg.get("print_every", 1))

    # timeout para limpar grupos incompletos
    group_timeout_seconds = int(cfg.get("group_timeout_seconds", 30))

    print(f"[BOOT] sse_url={sse_url}", flush=True)
    print(f"[BOOT] cloud_url={cloud_url}", flush=True)
    print("[BOOT] modo consolidado: envia apenas quando fechar todos os pacotes", flush=True)

    count = 0
    groups = {}

    while True:
        try:
            print(f"[SSE] conectando: {sse_url}", flush=True)

            for line in sse_lines(sse_url, read_timeout=60):
                if not line:
                    continue

                if not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()

                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    print(f"[WARN] json inválido: {e}", flush=True)
                    continue

                data_type = (evt.get("dataType") or "").lower()
                gateway_mac = get_gateway_mac(evt)
                device_mac = get_device_mac(evt)
                handle = evt.get("handle")
                hex_value = (evt.get("value") or "").strip()

                if not hex_value:
                    print(f"[INFO] evento sem value dev={device_mac} type={data_type}", flush=True)
                    continue

                if not is_hex_string(hex_value):
                    print(f"[WARN] value não é hex válido device={device_mac} handle={handle}", flush=True)
                    continue

                try:
                    frame = parse_frame(hex_value)
                except Exception as e:
                    print(f"[WARN] erro parse frame device={device_mac}: {e}", flush=True)
                    continue

                sensor_id = frame["sensor"]
                packet_number = frame["packetNumber"]
                total_packets = frame["totalPackets"]

                count += 1

                print(
                    f"[FRAME] #{count} type={data_type} dev={device_mac or '-'} "
                    f"sensor={sensor_id} packet={packet_number}/{total_packets} "
                    f"samples={frame['sampleCount']} trailing={frame['trailingHex'] or '-'}"
                , flush=True)

                group_key = make_group_key(gateway_mac, device_mac, sensor_id)

                if group_key not in groups:
                    groups[group_key] = create_group(evt, frame)

                group = groups[group_key]
                group["lastUpdateAt"] = time.time()

                # Se o total mudar no meio, loga
                if group["expectedPackets"] != total_packets:
                    print(
                        f"[WARN] totalPackets mudou no grupo {group_key}: "
                        f"{group['expectedPackets']} -> {total_packets}"
                    , flush=True)
                    group["expectedPackets"] = total_packets

                # sobrescreve se pacote duplicado chegar novamente
                is_duplicate = packet_number in group["packets"]
                group["packets"][packet_number] = frame

                received_count = len(group["packets"])
                expected_count = group["expectedPackets"]

                if count % print_every == 0:
                    print(
                        f"[GROUP] dev={device_mac or '-'} sensor={sensor_id} "
                        f"recebidos={received_count}/{expected_count} "
                        f"ultimo_pacote={packet_number} duplicado={is_duplicate}"
                    , flush=True)

                # Fechou o grupo => envia 1 body só
                if received_count >= expected_count:
                    payload = build_consolidated_payload(group)

                    preview = {
                        "gatewayMac": payload["gatewayMac"],
                        "deviceMac": payload["deviceMac"],
                        "sensor": payload["sensor"],
                        "packetCount": payload["packetCount"],
                        "expectedPackets": payload["expectedPackets"],
                        "sampleCount": payload["sampleCount"],
                    }
                    print(f"[GROUP-READY] {json.dumps(preview, ensure_ascii=False)}", flush=True)

                    try:
                        post_json(cloud_url, payload, token=token, timeout=timeout)
                        print(
                            f"[OK] grupo consolidado enviado "
                            f"dev={payload['deviceMac'] or '-'} "
                            f"sensor={payload['sensor']['id']} "
                            f"pacotes={payload['packetCount']} "
                            f"amostras={payload['sampleCount']}"
                        , flush=True)
                    except Exception as e:
                        print(f"[WARN] erro ao postar grupo consolidado: {e}", flush=True)

                    # limpa o grupo após envio
                    del groups[group_key]

                # limpeza de grupos velhos/incompletos
                now_ts = time.time()
                expired_keys = []
                for key, grp in groups.items():
                    if now_ts - grp["lastUpdateAt"] > group_timeout_seconds:
                        expired_keys.append(key)

                for key in expired_keys:
                    grp = groups[key]
                    print(
                        f"[TIMEOUT] descartando grupo incompleto "
                        f"dev={grp['deviceMac'] or '-'} sensor={grp['sensorId']} "
                        f"recebidos={len(grp['packets'])}/{grp['expectedPackets']}"
                    , flush=True)
                    del groups[key]

        except Exception as e:
            print(f"[WARN] SSE caiu/erro: {e} (reconectando em 2s)", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()