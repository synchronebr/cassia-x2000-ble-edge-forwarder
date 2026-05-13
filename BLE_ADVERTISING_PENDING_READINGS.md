# Leituras Pendentes no Advertising BLE — Implementação Atual do Firmware

## Contexto

O firmware atual do sensor Synchroone (STM32WBA65) indica leituras pendentes
via **Service Data** no payload de advertising BLE. Essa é a implementação
que existe hoje em `app_ble.c`, sem necessidade de nenhuma mudança no firmware.

---

## Como o Firmware Monta o Payload

A função `update_adv_data()` em `STM32_WPAN/App/app_ble.c` constrói três
AD structures no array `a_AdvData`:

### AD Structure 1 — Local Name

```
05  09  53 79 6E 63
│   │   └──────────── "Sync" em ASCII
│   └─ AD Type: 0x09 (Complete Local Name)
└── length = 5
```

### AD Structure 2 — Service Data UUID 0x5140 (UID do hardware)

```
0F  16  40 51  [12 bytes do UID do STM32]
│   │   └──── UUID 0x5140 em little-endian
│   └── AD Type: 0x16 (Service Data)
└── length = 15
```

O UID são os 96 bits únicos do STM32 lidos de `LL_GetUID_Word0/1/2()`.
Identifica unicamente cada unidade de hardware.

### AD Structure 3 — Service Data UUID 0x5141 (leituras pendentes)

```
04  16  41 51  [stored_frame_counter]
│   │   └──── UUID 0x5141 em little-endian
│   └── AD Type: 0x16 (Service Data)
└── length = 4
```

**Este é o campo que sinaliza leituras pendentes.**

---

## O Campo `stored_frame_counter`

É um único byte, uint8, que conta quantos frames de leitura estão
armazenados na memória do sensor aguardando coleta:

| Valor | Significado | Ação do edge service |
|-------|-------------|----------------------|
| `0x00` | Sensor idle — sem dados | Ignora, não conecta |
| `0x01`–`0xFF` | N frames aguardando coleta | Conecta e coleta |

O firmware incrementa o contador quando um frame não pode ser transmitido
imediatamente (estado `FRAME_STORE`) e o decrementa após transmissão
bem-sucedida de cada frame armazenado.

---

## Exemplo de Payload

Payload hex completo recebido no campo `adv_data` do evento SSE `/gap/nodes`:

```
05 09 53 79 6E 63
0F 16 40 51 A1 B2 C3 D4 E5 F6 07 08 09 0A 0B 0C
04 16 41 51 02
```

Decodificando:
- `05 09 53 79 6E 63` → nome "Sync"
- `0F 16 40 51 A1B2C3D4 E5F607080 90A0B0C` → UUID 0x5140, UID = `a1b2c3d4e5f607080 90a0b0c`
- `04 16 41 51 02` → UUID 0x5141, `stored_frame_counter = 2` (dois frames pendentes)

---

## Como o Edge Service Lê Esse Campo

Em `sync_reading.1.8/opt/sync_reading/lib/adv_parser.py`:

```python
_SERVICE_UUID_DEVICE_UID  = 0x5140
_SERVICE_UUID_PENDING_CNT = 0x5141

def parse_synchroone_adv(adv_data_hex: str):
    data = bytes.fromhex(adv_data_hex)

    pending_count = None
    uid_hex = None

    i = 0
    while i < len(data):
        length  = data[i]
        ad_type = data[i + 1]
        payload = data[i + 2 : i + 1 + length]

        if ad_type == 0x16 and len(payload) >= 2:
            uuid = struct.unpack_from('<H', payload, 0)[0]   # little-endian

            if uuid == _SERVICE_UUID_PENDING_CNT and len(payload) >= 3:
                pending_count = payload[2]       # stored_frame_counter

            elif uuid == _SERVICE_UUID_DEVICE_UID and len(payload) >= 14:
                uid_hex = payload[2:14].hex()    # UID do STM32 (12 bytes)

        i += 1 + length

    if pending_count is None or pending_count == 0:
        return None   # sensor idle — não conectar

    return {
        'pending_count': pending_count,
        'seq_num':       pending_count,
        'uid':           uid_hex,
    }
```

`pending_count == 0` ou campo ausente → retorna `None` → connection_manager
descarta o evento sem abrir conexão.

`pending_count > 0` → connection_manager agenda a conexão BLE.

---

## Como o Scan é Configurado

Em `config.json`:

```json
"scan_path": "/gap/nodes?event=1&filter_name=Sync&filter_duplicates=0&filter_rssi=-90"
```

| Parâmetro | Efeito |
|-----------|--------|
| `filter_name=Sync` | Descarta todos os dispositivos BLE do ambiente que não tenham "Sync" no nome |
| `filter_duplicates=0` | Encaminha todos os eventos do mesmo sensor, mesmo que o payload não tenha mudado |
| `filter_rssi=-90` | Descarta sensores com sinal muito fraco para completar o TX |

> **Nota:** Não usamos `filter_manufacturer_data` porque o firmware usa
> Service Data (`0x16`), não Manufacturer Specific Data (`0xFF`).
> O filtro de fabricante teria bloqueado todos os eventos.

A decisão de conectar ou não é feita **no Python**, pelo `adv_parser.py`,
lendo o `stored_frame_counter` de cada evento recebido.

---

## Ciclo Completo de um Evento

```
Sensor                            Cassia X2000           Edge Service (Python)
  │                                    │                        │
  │── advertising com UUID 0x5141=0 ──>│ descartado pelo        │
  │   (idle, sem dados)                │ filter_name            │
  │                                    │                        │
  │   [frame armazenado na memória]    │                        │
  │                                    │                        │
  │── advertising com UUID 0x5141=1 ──>│ passa os filtros       │
  │   stored_frame_counter = 1         │──── SSE event ────────>│
  │                                    │          scan_reader recebe adv_data
  │                                    │          adv_parser: pending_count=1
  │                                    │          connection_manager: conectar
  │<─── POST /gap/nodes/{mac}/connection ──────────────────────>│
  │── GATT TX: START→TIMESTAMP→dados──>│── notificações GATT ──>│
  │── GATT TX: END FRAME ─────────────>│                        │
  │                                    │          assembler: frame completo
  │                                    │          tx_done_queue sinaliza
  │<─── DELETE /gap/nodes/{mac}/connection ────────────────────>│
  │                                    │                        │
  │── advertising com UUID 0x5141=0 ──>│                        │
  │   (counter decrementado, idle)     │                        │
```

---

## Diferença em Relação ao Formato Planejado

O `BLE_ADVERTISING_SPEC.md` documenta o formato **futuro** baseado em
Manufacturer Specific Data (`AD Type 0xFF`), que permite filtrar sensores
idle diretamente no Cassia via `filter_manufacturer_data=FFFF`.

O formato **atual** usa Service Data (`AD Type 0x16`), que não tem suporte
ao filtro `filter_manufacturer_data` no Cassia. Por isso o edge service
recebe todos os eventos de sensores Sync e aplica o filtro `pending_count > 0`
internamente no `adv_parser.py`.
