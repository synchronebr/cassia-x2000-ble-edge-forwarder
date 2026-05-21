# BLE Sensor Reading Service — Plano de Implementação

## Contexto

O app já possui toda a infraestrutura BLE necessária:
- `BLEManagerContext` / `useBLEManager` — scanning, connect, disconnect
- `useFirmwareUpdate` — exemplo completo de `monitorCharacteristicForService` + pattern de unlock CONFIG
- UUIDs confirmados em `src/hooks/useFirmwareUpdate.ts`

A tela `MeasuringPointOnlinePage` já tem o botão **"Solicitar Leitura"** (quando `deviceStatus === Active`), mas ainda não faz nada. Esse é o ponto de entrada da feature.

---

## UUIDs Confirmados (extraídos de `useFirmwareUpdate.ts`)

```
Padrão: a839XXXX-1b1c-4bdf-94d7-294d82389488

CONFIG_SERVICE          a8390100-1b1c-4bdf-94d7-294d82389488
CONFIG_PASSWORD_CHAR    a8390103-1b1c-4bdf-94d7-294d82389488
CONFIG_ADDRESS_CHAR     a8390101-1b1c-4bdf-94d7-294d82389488
CONFIG_VALUE_CHAR       a8390102-1b1c-4bdf-94d7-294d82389488

DATA_SERVICE            a8390200-1b1c-4bdf-94d7-294d82389488
DATA_ARRAY_CHAR         a8390201-1b1c-4bdf-94d7-294d82389488   ← indications
DATA_HASH_CHAR          a8390202-1b1c-4bdf-94d7-294d82389488
DATA_REQUEST_CHAR       a8390203-1b1c-4bdf-94d7-294d82389488   ← write 1 byte (trigger)
```

---

## Protocolo BLE do Sensor (STM32WBA)

### Header fixo de 7 bytes (todos os pacotes)

| Offset | Tamanho | Tipo        | Campo        |
|--------|---------|-------------|--------------|
| 0      | 1       | uint8       | packetId     |
| 1–2    | 2       | uint16 **BE** | packetNumber (1-indexed) |
| 3–4    | 2       | uint16 **BE** | totalPackets |
| 5–6    | 2       | uint16 **BE** | totalBytes   |
| 7+     | var     | —           | payload      |

### Packet IDs ativos

| ID   | Tipo        | Payload                                       |
|------|-------------|-----------------------------------------------|
| 0x00 | START       | ASCII "START FRAME" (descartável)             |
| 0x01 | TIMESTAMP   | uint32 LE, primeiros 4 bytes                  |
| 0x02 | ACCEL       | N × 6 bytes: `[x_lo, x_hi, y_lo, y_hi, z_lo, z_hi]` int16 LE |
| 0x06 | END         | ASCII "END FRAME" — sinaliza frame completo   |

### Volume de dados típico (config padrão)

- Janela de amostragem: 300 ms
- Taxa: 26.667 Hz → **~8.000 amostras**
- Payload accel: 8.000 × 6 = **48.000 bytes**
- MTU padrão: 247 bytes → dados úteis por pacote: 240 bytes (= 40 amostras)
- Total de pacotes accel: ceil(8.000 ÷ 40) = **200 pacotes**
- Firmware aguarda **indication confirmation** antes de enviar o próximo pacote

---

## O que criar

```
src/
├── utils/
│   └── ble/
│       ├── bleProtocol.ts          ← constantes de UUIDs + packet IDs
│       ├── blePacket.ts            ← parse do header 7 bytes + decode por tipo
│       └── frameAssembler.ts       ← reassembly da sequência → SensorFrame completo
├── services/
│   └── SensorData/
│       └── index.ts                ← POST para a API com a leitura montada
└── hooks/
    └── useSensorReader/
        └── index.ts                ← hook orquestrador (conectar → subscrever → montar → enviar)
```

E uma **modificação** em:
```
src/screens/MeasurementPointDetails/MeasuringPointOnlinePage/index.tsx
```

---

## Detalhes de cada arquivo

### `src/utils/ble/bleProtocol.ts`

Exporta todas as constantes:

```ts
export const BLE_UUIDS = {
  CONFIG_SERVICE:       'a8390100-1b1c-4bdf-94d7-294d82389488',
  CONFIG_PASSWORD_CHAR: 'a8390103-1b1c-4bdf-94d7-294d82389488',
  CONFIG_ADDRESS_CHAR:  'a8390101-1b1c-4bdf-94d7-294d82389488',
  CONFIG_VALUE_CHAR:    'a8390102-1b1c-4bdf-94d7-294d82389488',
  DATA_SERVICE:         'a8390200-1b1c-4bdf-94d7-294d82389488',
  DATA_ARRAY_CHAR:      'a8390201-1b1c-4bdf-94d7-294d82389488',
  DATA_REQUEST_CHAR:    'a8390203-1b1c-4bdf-94d7-294d82389488',
} as const;

export const PACKET_ID = {
  START:     0x00,
  TIMESTAMP: 0x01,
  ACCEL:     0x02,
  MAG:       0x03,
  TEMP:      0x04,
  MIC:       0x05,
  END:       0x06,
} as const;

export const UNLOCK_PASSWORD = 5000;
export const HEADER_SIZE = 7; // bytes
export const ASSEMBLY_TIMEOUT_MS = 30_000;
```

---

### `src/utils/ble/blePacket.ts`

Parse de um buffer (Uint8Array) recebido via indication:

```ts
export type ParsedPacket =
  | { type: 'start' }
  | { type: 'timestamp'; value: number }
  | { type: 'accel'; samples: Array<{ x: number; y: number; z: number }> }
  | { type: 'end' }
  | { type: 'unknown'; packetId: number };

export type PacketHeader = {
  packetId: number;
  packetNumber: number;   // 1-indexed
  totalPackets: number;
  totalBytes: number;
  parsed: ParsedPacket;
};

export function parsePacket(buf: Uint8Array): PacketHeader
```

**Lógica interna:**
- `buf[0]` → packetId
- `buf[1]<<8 | buf[2]` → packetNumber (BE)
- `buf[3]<<8 | buf[4]` → totalPackets (BE)
- `buf[5]<<8 | buf[6]` → totalBytes (BE)
- payload → `buf.slice(7)`

Decode do payload por packetId:
- `0x01 TIMESTAMP`: `DataView.getUint32(0, true)` (LE)
- `0x02 ACCEL`: loop a cada 6 bytes → `getInt16(i, true)` para x, y, z
- Outros: retorna `unknown`

---

### `src/utils/ble/frameAssembler.ts`

Acumula pacotes de uma única sessão BLE ativa:

```ts
export type SensorFrame = {
  timestamp: number;
  accel: {
    x: number[];
    y: number[];
    z: number[];
    sampleCount: number;
  };
  receivedPackets: number;
  totalPackets: number;
  readingAt: string; // ISO8601
};

export class FrameAssembler {
  push(packet: PacketHeader): SensorFrame | null
  reset(): void
  isExpired(): boolean  // > ASSEMBLY_TIMEOUT_MS sem END
}
```

**Estado interno:**
- `hasStart`, `hasEnd`, `hasTimestamp`: boolean
- `timestamp`: number
- `accelSamples`: Array<{x,y,z}>
- `packetCount`: number, `totalPackets`: número do primeiro pacote com totalPackets > 0
- `startedAt`: Date (para timeout)

**Regra de completude:**  
`hasStart && hasTimestamp && hasEnd && packetCount === totalPackets` → retorna `SensorFrame`, chama `reset()`

---

### `src/services/SensorData/index.ts`

```ts
export async function postSensorReading(
  companyId: number,
  deviceCode: string,
  frame: SensorFrame
): Promise<void>
```

**Payload para a API** (a confirmar com o backend):
```json
{
  "deviceCode": "SYNC-0001",
  "readingAt": "2026-04-12T...",
  "timestamp": 123456789,
  "accel": {
    "x": [...],
    "y": [...],
    "z": [...],
    "sampleCount": 8000
  }
}
```

> **PENDÊNCIA:** Confirmar o endpoint exato com o time de backend.  
> Candidato provável: `POST /sensors/mobile` ou `POST /readings/ble`

---

### `src/hooks/useSensorReader/index.ts`

Hook orquestrador. Recebe `deviceCode` e `companyId`.

```ts
export type SensorReaderStatus =
  | 'idle'
  | 'connecting'
  | 'subscribing'
  | 'receiving'   // recebendo pacotes
  | 'sending'     // POST para API
  | 'success'
  | 'error';

export function useSensorReader(companyId: number, deviceCode: string) {
  return {
    status: SensorReaderStatus,
    progress: number,        // 0–100 baseado em packetNumber/totalPackets
    errorMessage: string,
    startReading: () => Promise<void>,
    cancel: () => void,
  };
}
```

**Sequência interna do `startReading()`:**

1. `setStatus('connecting')`
2. `connectDeviceByName('Sync')` (via `useBLEManager`, padrão do `useFirmwareUpdate`)
3. `setStatus('subscribing')`
4. Negociar MTU 512 se possível: `device.requestMTU(512)`
5. `device.monitorCharacteristicForService(DATA_SERVICE, DATA_ARRAY_CHAR, callback)`
6. `setStatus('receiving')`
7. Callback por indication:
   - `Buffer.from(char.value, 'base64')` → `Uint8Array`
   - `parsePacket(buf)` → `PacketHeader`
   - `assembler.push(packet)` → `SensorFrame | null`
   - Atualiza `progress` com `packet.packetNumber / packet.totalPackets * 100`
   - Se `SensorFrame` retornado → cai no passo 8
   - Se `assembler.isExpired()` → erro com "Timeout de montagem de frame"
8. `setStatus('sending')`
9. `postSensorReading(companyId, deviceCode, frame)`
10. `setStatus('success')`
11. `device.cancelConnection()`

**Tratamento de erros:** qualquer throw → `setStatus('error')`, `setErrorMessage(e.message)`, disconnect

---

### Modificação em `MeasuringPointOnlinePage`

O botão "Solicitar Leitura" (linha ~510) atualmente é:
```tsx
<TouchableOpacity style={styles.actionBtn}>
  <Text style={styles.actionBtnText}>{t("index.requestReading")}</Text>
</TouchableOpacity>
```

**Passa a ser:**
```tsx
// Instancia o hook (acima do return)
const { status, progress, startReading, cancel } = useSensorReader(
  user?.currentCompany?.companyId,
  device.code
);

// Botão com estado:
<TouchableOpacity
  style={styles.actionBtn}
  onPress={status === 'receiving' ? cancel : startReading}
  disabled={status === 'connecting' || status === 'subscribing' || status === 'sending'}
>
  {status === 'receiving' ? (
    <Text style={styles.actionBtnText}>{progress}% — Cancelar</Text>
  ) : status === 'sending' ? (
    <Text style={styles.actionBtnText}>Enviando...</Text>
  ) : (
    <Text style={styles.actionBtnText}>{t("index.requestReading")}</Text>
  )}
</TouchableOpacity>
```

Após `success`: chamar `getReadingAnalytics` novamente para atualizar os cards.  
Após `error`: `Toast.show(errorMessage, { type: 'danger' })`.

---

## Perguntas a resolver antes de implementar

| # | Pergunta | Impacto |
|---|----------|---------|
| 1 | Qual endpoint recebe a leitura do mobile? (`/sensors/mobile`? `/readings/ble`?) | `SensorData/index.ts` |
| 2 | O `DATA_REQUEST_CHAR` (`0xa8390203`) precisa ser escrito para **disparar** uma leitura, ou o sensor já envia automaticamente ao se conectar? | Sequência do `startReading` |
| 3 | O `connectDeviceByName('Sync')` do `useBLEManager` é suficiente, ou o device.code tem o nome exato do advertising? | Passo 2 do hook |
| 4 | Precisamos de MTU negociado ou 247 já é suficiente para o caso mobile? | Performance (200 vs ~100 pacotes) |

---

## Ordem de implementação

1. `bleProtocol.ts` — apenas constantes, sem lógica
2. `blePacket.ts` — parser puro (testável isolado)
3. `frameAssembler.ts` — classe de estado puro (testável isolado)
4. `SensorData/index.ts` — service de API (depende do endpoint confirmado)
5. `useSensorReader/index.ts` — hook orquestrador (junta tudo)
6. Modificação em `MeasuringPointOnlinePage` — integração visual

---

## Notas técnicas

- **Byte order:** header usa **big-endian** (BE), payloads de sensor usam **little-endian** (LE)
- **Indication vs Notification:** o firmware usa `indicate` (com ACK). O `react-native-ble-plx` trata ambos via `monitorCharacteristicForService` — sem diferença na API do lado mobile
- **Buffer decode:** igual ao OTA — `Buffer.from(char.value, 'base64')` → converte para `Uint8Array` via `new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength)`
- **Cancelamento:** ao cancelar ou dar erro, sempre chamar `device.cancelConnection()` e `subscription.remove()`
- **Conexão BLE:** seguir o mesmo padrão do `useFirmwareUpdate` — `connectDeviceByName` já faz discover services + características
