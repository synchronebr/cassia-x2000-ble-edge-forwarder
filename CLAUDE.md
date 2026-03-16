# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Context (Synchroone)

This is an **edge data acquisition service** for the Cassia X2000 BLE gateway, part of the Synchroone platform. The full data path is:

```
BLE Sensors (firmware/C)
      ↓  BLE GATT notifications
Cassia X2000 Gateway
      ↓  SSE (HTTP streaming)
This Service (Python, edge container)
      ↓  HTTP POST JSON
Cloud Backend (Go)
```

The Python edge service is responsible for: receiving BLE packet events, reassembling fragmented multi-packet frames, decoding sensor payloads, persisting events to disk, and forwarding complete sensor readings to the Go cloud backend.

## Running the Application

```bash
# Run locally (no pip dependencies — Python 3 stdlib only)
cd sync_reading.1.7/opt/sync_reading
python3 app.py

# Package a release
tar -zcvf sync_reading.1.X.tar.gz sync_reading.1.X/
```

## Deploying to Gateway

1. Copy the `.tar.gz` to the Cassia X2000 gateway
2. Access via: Gateway UI > Container > Container Operation > Remote Login
   - Login: `cassia` / Pass: `cassia-XXXXXX` (last 6 chars of gateway MAC, e.g. `cassia-e59684`)
3. Extract and start `autorun.sh` — it acts as the process supervisor with auto-restart and log rotation

## Viewing Logs on Gateway

```bash
cat /opt/sync_reading/logs/app.log
rm -f /opt/sync_reading/logs/app.log   # clear logs
```

Logs are **JSONL** (one JSON object per line). `autorun.sh` rotates at 5MB, keeping up to 6 files (`.log.1`–`.log.6`).

## Configuration

`sync_reading.1.7/opt/sync_reading/config.json`:

| Field | Description |
|-------|-------------|
| `gateway_api_base` | Gateway URL (e.g. `http://10.10.10.254`) |
| `sse_path` | SSE endpoint for GATT notifications |
| `cloud_url` | Go backend base URL |
| `cloud_ingest_path` | Cloud ingest path (e.g. `/sensors/cassia`) |
| `api_key` | API key for cloud auth (optional) |
| `api_key_header` | Header name for API key (default `X-API-Key`) |
| `batch_size` | Spool write batch size |
| `flush_interval_seconds` | Cloud send interval |
| `spool_max_mb` | Max disk queue size (MB) |
| `assembly_timeout_seconds` | Max wait for incomplete packet assembly |
| `max_open_assemblies` | Max concurrent in-flight assemblies |
| `packet_queue_max` | In-memory packet buffer size (default 20000) |
| `outbound_queue_max` | In-memory event buffer size (default 5000) |
| `cassia_info_required` | `1` = fail on boot if gateway identity unavailable |

## Architecture — Processing Pipeline

5 concurrent daemon threads:

```
Gateway SSE stream (GATT notifications)
      ↓
[SSE Reader] ──→ packet_queue (max 20k)
                        ↓
                 [Assembler] ──→ outbound_queue (max 5k)
                                       ↓              ↓
                              [Spool Writer]    [Cloud Sender → Go HTTP POST]
                                                [Metrics logger every 5s]
```

**SSE Reader** ([lib/sse.py](sync_reading.1.7/opt/sync_reading/lib/sse.py)): Connects to gateway SSE endpoint, parses GATT notification events, extracts device MAC, AP MAC, and hex-encoded payload value. Reconnects with exponential backoff capped at 30s.

**Assembler** ([lib/assembly.py](sync_reading.1.7/opt/sync_reading/lib/assembly.py)): Groups fragmented packets by `{device}:{timestamp_bucket}:{packet_id}`. A valid frame requires START + TIMESTAMP + sensor data packets + END. Times out incomplete assemblies; produces error events with diagnostics (missing packet list, bytes received, assembly age).

**BLE Packet Parser** ([lib/ble_packet.py](sync_reading.1.7/opt/sync_reading/lib/ble_packet.py)): Decodes hex payloads into structured sensor readings. Handles all 7 packet types (see protocol below).

**Spool** ([lib/spool.py](sync_reading.1.7/opt/sync_reading/lib/spool.py)): File-based JSONL queue at `/opt/sync_reading/spool/events_pending.jsonl`. Atomic writes via `os.replace()`. Drops oldest events when `spool_max_mb` is exceeded.

**Cloud Sender** ([lib/http_client.py](sync_reading.1.7/opt/sync_reading/lib/http_client.py)): POSTs events one-by-one to the Go backend. Re-queues failed items back into spool.

**Gateway Identity** ([lib/cassia_info.py](sync_reading.1.7/opt/sync_reading/lib/cassia_info.py)): Fetches gateway MAC, IP, model, firmware at startup via gateway REST API.

**RSSI** ([lib/rssi.py](sync_reading.1.7/opt/sync_reading/lib/rssi.py)): Thread-safe cache tracking per-device signal strength from `/gap/rssi`.

## BLE Packet Protocol

Every packet has a **7-byte fixed header**, followed by payload:

| Bytes | Field | Description |
|-------|-------|-------------|
| 0 | `packetId` / `sensorId` | Identifies packet type (see table below) |
| 1–2 | `packetNumber` (H, L) | Current packet index in this block |
| 3–4 | `totalPackets` (H, L) | Total packets in this block |
| 5–6 | `totalBytes` (H, L) | Total size of this packet (header + payload) |
| 7+ | payload | Type-specific content |

### Packet Type IDs

| ID | Name | Payload |
|----|------|---------|
| `0x00` | `BLE_TX_START_MESSAGE` | ASCII string `"START FRAME"` |
| `0x01` | `BLE_TX_TIMESTAMP` | uint32 or uint64, little-endian |
| `0x02` | `BLE_TX_SENSOR_IIS3DWB` | Accelerometer samples (see below) |
| `0x03` | `BLE_TX_SENSOR_IIS2MDC` | Magnetometer samples (see below) |
| `0x04` | `BLE_TX_SENSOR_STTS22H` | Temperature: int16 LE, value ÷ 100 = °C |
| `0x05` | `BLE_TX_SENSOR_IMP23ABSU` | Microphone PCM, 16-bit LE samples |
| `0x06` | `BLE_TX_END_MESSAGE` | ASCII string `"END FRAME"` |

**Currently active in firmware**: `0x00`, `0x01`, `0x02`, `0x06`. Types `0x03`, `0x04`, `0x05` exist in the protocol but are not yet transmitted (sensors not configured).

### Sensor Sample Layout (IIS3DWB and IIS2MDC)

Each sample is **6 bytes, little-endian**, axis order: X, Y, Z:

```
[xl, xh, yl, yh, zl, zh]
```

Decode each axis: `value = int16(hi << 8 | lo)` (signed, 2's complement).

**Transmission order within a frame**: `START → TIMESTAMP → sensor packets → END`

When a block is fragmented, use `packetNumber` and `totalPackets` to reassemble before decoding.

## BLE Log Codes

The firmware also sends 4-byte integer log codes over a separate BLE characteristic (not GATT data). Used for monitoring firmware health:

| Range | Category |
|-------|----------|
| 1000–1001 | General events (`EVENT_NONE`, `START_MAIN_TASK`) |
| 2000 | General errors (`ERROR_NONE`) |
| 3000–3004 | IIS3DWB events: NONE, START, DATA_READ, WINDOW_FULL, STOP |
| 4000–4007 | IIS3DWB errors: NONE, START, RESTART_START, RESTART_READ, READ_IO, READ_TIMEOUT, STOP, RESTART_STOP |
| 5000–5002 | BLE TX events: NONE, START, DONE |
| 6000–6002 | BLE TX errors: NONE, GENERIC, TIMEOUT |

## Active Development Directory

`sync_reading.1.7/` is the current working source. The `.tar.gz` files are release snapshots — do not edit them. There is no formal test suite; validation is done against real Cassia X2000 hardware.

When working on payload parsing or assembly logic, keep in mind:
- All multi-byte integers are **little-endian** unless noted otherwise
- Timestamp format is not yet finalized in firmware — the parser handles multiple formats defensively
- The assembler uses a 30-second time bucket to group packets from the same transmission window
