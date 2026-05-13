#!/usr/bin/env python3
"""
Setup de sessão GATT após conexão BLE com sensor Synchroone.

Fluxo obrigatório após connect_device() OK:
  1. Ler INFO/SN       → UID do STM32 (12 bytes, sem autenticação)
  2. Calcular senha    → TEA com chaves privadas do firmware
  3. Escrever PASSWORD → autentica sessão no firmware
  4. Ler SLOT0/SLOT1   → verificar se outro gateway do mesmo tipo já está conectado
  5. Escrever TYPE     → firmware reserva um slot para este gateway
  6. Escrever CCCD     → habilita indications de dados (DATA_ARRAY_VALUE)
  7. Escrever REQUEST  → DATA_REQUEST_ALL_DATA (0x01) dispara SYNCHROONE_RoutineTriggerStoredTx()

Retorna True  — sessão pronta, sensor enviará dados.
Retorna False — slot ocupado por outro gateway do mesmo tipo (caller deve desconectar).
Lança RuntimeError em qualquer falha de comunicação GATT.
"""
import json
import struct

from lib.cassia_api import read_handle, write_handle, pair_device
from lib.log import jlog

SERVICE = "sync_reading"

# Handles fixos — confirmados em campo (ConnectDeviceService.ts):
# "handles resolvidos — SN:20 SLOT0:38 SLOT1:41 TYPE:44 PASSWORD:54 ADDRESS:48 VALUE:51 DATA_ARRAY_VALUE:58 NOTIFY:59"
# DATA service layout (data.c): ARRAY(58)+CCCD(59)+UserDesc(60) | SFC(61-63) | REQUEST(64-66)
HANDLE_SN       = 20   # INFO/SN:       12 bytes LE (UID Word0+Word1+Word2) — sem auth
HANDLE_SLOT0    = 38   # INFO/SLOT_0:   1 byte — tipo da central no slot 0
HANDLE_SLOT1    = 41   # INFO/SLOT_1:   1 byte — tipo da central no slot 1
HANDLE_TYPE     = 44   # INFO/TYPE:     write 1 byte — firmware aloca slot automaticamente
HANDLE_PASSWORD = 54   # CONFIG/PASSWORD: write uint32 LE — autentica sessão
HANDLE_CCCD     = 59   # DATA/ARRAY (58) CCCD — habilita indications
HANDLE_REQUEST  = 65   # DATA/REQUEST value — write 0x01 = DATA_REQUEST_ALL_DATA

# Tipo desta central registrado nos slots do firmware.
# 66 decimal (0x42) — deve coincidir com OUR_CENTRAL_TYPE do firmware (ble_session.c).
OUR_CENTRAL_TYPE = 66

# Chaves TEA privadas do firmware (ble_session.c — BLE_SESSION_PASSWORD_KEY*).
# Replicadas aqui para gerar a senha dinâmica sem comunicação extra.
_KEY0 = 0x7A7589AA
_KEY1 = 0x46E1B2AB
_KEY2 = 0x2A66F41D
_KEY3 = 0xB30D177B
_TEA_DELTA  = 0x9E3779B9
_TEA_ROUNDS = 32


# ── geração de senha ──────────────────────────────────────────────────────────

def _rol32(value: int, shift: int) -> int:
    v = value & 0xFFFFFFFF
    return ((v << shift) | (v >> (32 - shift))) & 0xFFFFFFFF


def generate_unlock_password(uid0: int, uid1: int, uid2: int) -> int:
    """
    Replica generateUnlockPassword() de ble_session.c (firmware STM32WBA65).

    uid0, uid1, uid2: três uint32 lidos de INFO/SN como little-endian.
    Retorna uint32 a ser escrito em CONFIG/PASSWORD como 4 bytes LE.
    """
    v0 = uid0 & 0xFFFFFFFF
    v1 = uid1 & 0xFFFFFFFF
    v2 = uid2 & 0xFFFFFFFF

    k0 = (_KEY0 ^ v2)            & 0xFFFFFFFF
    k1 = (_KEY1 ^ _rol32(v2,  7)) & 0xFFFFFFFF
    k2 = (_KEY2 ^ _rol32(v2, 13)) & 0xFFFFFFFF
    k3 = (_KEY3 ^ _rol32(v2, 19)) & 0xFFFFFFFF

    s = 0
    for _ in range(_TEA_ROUNDS):
        s  = (s + _TEA_DELTA) & 0xFFFFFFFF
        v0 = (v0 + (((v1 << 4) + k0) ^ (v1 + s) ^ ((v1 >> 5) + k1))) & 0xFFFFFFFF
        v1 = (v1 + (((v0 << 4) + k2) ^ (v0 + s) ^ ((v0 >> 5) + k3))) & 0xFFFFFFFF

    return (v0 ^ _rol32(v1, 11) ^ _rol32(v2, 23)) & 0xFFFFFFFF


# ── setup principal ───────────────────────────────────────────────────────────

def gatt_setup(gateway_api: str, mac: str, timeout: int = 5, addr_type: str = "public") -> bool:
    """
    Executa o setup GATT completo logo após connect_device() retornar 2xx.
    Deve rodar em thread worker — bloqueia por várias chamadas HTTP sequenciais.

    Retorna True  — autenticado, slot alocado, indications habilitadas.
    Retorna False — slot ocupado por outro gateway do tipo OUR_CENTRAL_TYPE.
    Lança RuntimeError em falha de comunicação.
    """
    # 0. Pairing BLE (Just Works) — obrigatório antes de escrever em CONFIG/PASSWORD
    #    Endpoint: POST /management/nodes/{mac}/pair/ (disponível a partir da v2.1.1)
    pair_status, pair_body = pair_device(gateway_api, mac, addr_type=addr_type, timeout=15)
    if pair_status >= 400:
        raise RuntimeError("pair HTTP {}: {}".format(pair_status, pair_body[:120]))
    jlog(SERVICE, "INFO", "gatt_paired",
         "Link BLE autenticado (Just Works)",
         mac=mac, http_status=pair_status, response=pair_body[:120])

    # 1. Ler UID (INFO/SN — não requer autenticação prévia)
    sn_hex   = _read_value(gateway_api, mac, HANDLE_SN, timeout)
    sn_bytes = bytes.fromhex(sn_hex)
    if len(sn_bytes) != 12:
        raise RuntimeError(
            "INFO/SN: esperado 12 bytes, recebido {} ({})".format(len(sn_bytes), sn_hex)
        )

    uid0, uid1, uid2 = struct.unpack_from('<III', sn_bytes)
    password = generate_unlock_password(uid0, uid1, uid2)

    jlog(SERVICE, "DEBUG", "gatt_password_calc",
         "Senha dinâmica calculada a partir do UID",
         mac=mac,
         uid0=hex(uid0), uid1=hex(uid1), uid2=hex(uid2),
         password=hex(password))

    # 2. Autenticar sessão BLE (CONFIG/PASSWORD — uint32 LE, 4 bytes)
    _write(gateway_api, mac, HANDLE_PASSWORD, struct.pack('<I', password).hex(), timeout)

    # 3. Verificar slots — abortar se outro gateway do mesmo tipo já está conectado
    slot0 = _read_byte(gateway_api, mac, HANDLE_SLOT0, timeout)
    slot1 = _read_byte(gateway_api, mac, HANDLE_SLOT1, timeout)

    jlog(SERVICE, "INFO", "gatt_slots",
         "Slots verificados",
         mac=mac, slot0=slot0, slot1=slot1, our_type=OUR_CENTRAL_TYPE)

    if slot0 == OUR_CENTRAL_TYPE or slot1 == OUR_CENTRAL_TYPE:
        jlog(SERVICE, "WARN", "gatt_slot_occupied",
             "Sensor já tem gateway do mesmo tipo conectado",
             mac=mac, slot0=slot0, slot1=slot1, our_type=OUR_CENTRAL_TYPE)
        return False

    # 4. Registrar tipo desta central — firmware aloca slot automaticamente
    _write(gateway_api, mac, HANDLE_TYPE, format(OUR_CENTRAL_TYPE, '02x'), timeout)

    # 5. Habilitar indications no CCCD (0x0200 LE = BLE indicate enable)
    _write(gateway_api, mac, HANDLE_CCCD, '0200', timeout)

    # 6. Disparar transmissão dos dados armazenados (DATA_REQUEST_ALL_DATA = 0x01)
    #    Chama SYNCHROONE_RoutineTriggerStoredTx() no firmware — sem este write
    #    o sensor não inicia o TX mesmo com indications habilitadas.
    _write(gateway_api, mac, HANDLE_REQUEST, '01', timeout)

    jlog(SERVICE, "INFO", "gatt_session_ready",
         "Sessão GATT autenticada e pronta; TX de dados armazenados disparado",
         mac=mac)

    return True


# ── helpers internos ──────────────────────────────────────────────────────────

def _read_value(gateway_api: str, mac: str, handle: int, timeout: int) -> str:
    status, body = read_handle(gateway_api, mac, handle, timeout=timeout)
    if status >= 400:
        raise RuntimeError(
            "read handle {} HTTP {}: {}".format(handle, status, body[:120])
        )
    try:
        return json.loads(body).get("value", "").strip()
    except Exception:
        return body.strip()


def _read_byte(gateway_api: str, mac: str, handle: int, timeout: int) -> int:
    val = _read_value(gateway_api, mac, handle, timeout)
    if not val:
        return 0
    try:
        return int(val[:2], 16)
    except ValueError:
        raise RuntimeError("read handle {}: valor não-hex '{}'".format(handle, val[:20]))


def _write(gateway_api: str, mac: str, handle: int, value_hex: str, timeout: int):
    status, body = write_handle(gateway_api, mac, handle, value_hex, timeout=timeout)
    if status >= 400:
        raise RuntimeError(
            "write handle {} HTTP {}: {}".format(handle, status, body[:120])
        )
