#!/usr/bin/env python3
import struct

# Service Data UUIDs do firmware Synchroone (app_ble.c — update_adv_data)
# Payload de advertising montado pelo firmware (offset 0-based do adData):
#   offset  0–5   [05][09]['S','y','n','c']           Local Name "Sync"
#   offset  6–21  [0F][16][40][51][uid 12B]           Service Data UUID 0x5140 — UID do STM32
#   offset 22–26  [04][16][41][51][frame_ctr]         Service Data UUID 0x5141 — pending_count
#   offset 27–29  [02][01][06]                        Flags
#
# 0x5141 só está presente quando há leituras pendentes (firmware novo).
# O filtro Cassia filter_value={"offset":"22","data":"04164151"} garante que apenas
# sensores com 0x5141 chegam ao serviço — pending_count é sempre > 0 aqui.

_SERVICE_UUID_DEVICE_UID  = 0x5140
_SERVICE_UUID_PENDING_CNT = 0x5141


def parse_synchroone_adv(adv_data_hex: str):
    """
    Retorna dict se o pacote contém Service Data UUID 0x5141 (leituras pendentes).
    Retorna None se não for dispositivo Synchroone ou 0x5141 ausente.

    Com filter_value no scan Cassia, apenas pacotes com 0x5141 chegam aqui,
    portanto pending_count será sempre > 0.

    Campos do dict retornado:
      pending_count  — número de frames armazenados aguardando coleta (uint8)
      seq_num        — mesmo valor; usado pelo ConnectionManager para dedup
      uid            — UID do STM32 em hex (string de 24 chars) ou None
      error_flag     — False (reservado para versão futura com AD 0xFF)
      low_battery    — False (reservado para versão futura com AD 0xFF)
    """
    if not adv_data_hex:
        return None

    try:
        data = bytes.fromhex(adv_data_hex)
    except Exception:
        return None

    pending_count = None
    uid_hex = None

    i = 0
    while i < len(data):
        if i + 1 >= len(data):
            break
        length = data[i]
        if length == 0 or i + length >= len(data):
            break
        ad_type = data[i + 1]
        payload = data[i + 2: i + 1 + length]

        if ad_type == 0x16 and len(payload) >= 2:
            uuid = struct.unpack_from('<H', payload, 0)[0]
            if uuid == _SERVICE_UUID_PENDING_CNT and len(payload) >= 3:
                pending_count = payload[2]
            elif uuid == _SERVICE_UUID_DEVICE_UID and len(payload) >= 14:
                uid_hex = payload[2:14].hex()

        i += 1 + length

    if pending_count is None:
        return None

    return {
        'pending_count': pending_count,
        'seq_num':       pending_count,
        'uid':           uid_hex,
        'error_flag':    False,
        'low_battery':   False,
    }
