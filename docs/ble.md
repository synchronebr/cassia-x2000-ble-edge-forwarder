# Bluetooth LE (BLE)

Guia para trabalhar com Bluetooth LE no Synchroone. Implementação central em [src/contexts/BLEManagerContext.tsx](../src/contexts/BLEManagerContext.tsx).

---

## Stack

- **Biblioteca**: [`react-native-ble-plx`](https://github.com/dotintent/react-native-ble-plx) v3.5
- **Context**: `BLEManagerProvider` (global, em `App.tsx`)
- **Hook consumidor**: `useBLEManager()`

---

## API do context

```ts
interface IBLEManagerContext {
  // Scanning
  scanDevices(): Promise<Device[]>;
  isScanning: boolean;

  // Connection
  connectDeviceByName(name: string): Promise<Device>;
  connectDeviceById(id: string): Promise<Device>;
  disconnect(): Promise<void>;
  connectedDevice: Device | null;

  // Commands
  sendCommand(cmd: string): Promise<string>;
}
```

---

## Uso básico

### Scan
```tsx
import { useBLEManager } from '@/contexts/BLEManagerContext';

function MyScreen() {
  const { scanDevices, isScanning } = useBLEManager();

  async function handleScan() {
    const devices = await scanDevices();
    console.log('encontrados:', devices.length);
  }

  return <Button title="Scan" onPress={handleScan} disabled={isScanning} />;
}
```

### Connect + send command
```tsx
const { connectDeviceByName, sendCommand, disconnect } = useBLEManager();

await connectDeviceByName('SYN-12345');
const response = await sendCommand('GET_STATUS');
await disconnect();
```

---

## Fluxos por tela

### Pareamento de sensor (`DeviceSetup`)
1. User escaneia QR code (`QRCodeScanner`) — obtém ID do sensor
2. `connectDeviceById(id)` conecta
3. Envia comandos de configuração (`SET_INTERVAL`, `SET_COMPANY`, etc.)
4. Salva no backend via `services/Companies/Devices/`
5. Disconnect

### Update de firmware (`useFirmwareUpdate`)
Hook dedicado em [src/hooks/useFirmwareUpdate.ts](../src/hooks/useFirmwareUpdate.ts).
1. Baixa firmware do backend (`services/Firmware/`)
2. Divide em chunks
3. Envia via characteristics específicas
4. Valida checksum
5. Reinicia device

### Leitura ad-hoc (`DeviceTechLogs`)
Usado por técnicos para debug — conecta, lê logs, disconnect.

---

## Gotchas críticos

### 🚨 Single-flight scan
`BLE plx explode` se você iniciar scan enquanto outro está rodando. Proteção no context:

```ts
const isScanningRef = useRef(false);
const scanPromiseRef = useRef<Promise<Device[]> | null>(null);

async function scanDevices() {
  if (scanPromiseRef.current) return scanPromiseRef.current;
  isScanningRef.current = true;
  scanPromiseRef.current = actualScan();
  try {
    return await scanPromiseRef.current;
  } finally {
    isScanningRef.current = false;
    scanPromiseRef.current = null;
  }
}
```

**Não remova.** Se `scanDevices` parece demorado, o problema é outro.

### Retry com sleep entre disconnect/reconnect
iOS requer pausa de ~500-700ms entre operações sequenciais no mesmo device, senão dispara erro `OperationInProgress`. Solução no context:

```ts
await disconnect();
await sleep(600);
await connectDeviceById(id);
```

### UUIDs hardcoded
Service UUID e characteristic UUIDs são hardcoded no context (ex: `ab0828b1-...`, `4ac8a682-...`). São **determinísticos pelo hardware** — não podem ser "descobertos" dinamicamente sem overhead.

**Ao mudar firmware**: se UUIDs mudarem, coordenar com time de hardware **antes** de atualizar o app.

### Android: permissões duplas
Android 12+ separa:
- `BLUETOOTH_SCAN` (novo)
- `BLUETOOTH_CONNECT` (novo)
- `ACCESS_FINE_LOCATION` (legado mas ainda exigido em alguns OEMs)

Tudo declarado em `app.json` via expo-config. Se usuário negou, abra settings:

```ts
import { Linking } from 'react-native';
Linking.openSettings();
```

### iOS: Info.plist descriptions
`NSBluetoothAlwaysUsageDescription` deve explicar **por que** o app precisa de BLE. Já configurado via expo-config.

---

## RSSI / qualidade de sinal

Função utilitária: [src/utils/getSignalBLEQuality.ts](../src/utils/getSignalBLEQuality.ts).

```ts
// retorna 0-4 barras baseado em RSSI
const bars = getSignalBLEQuality(rssi); // -30 → 4 bars, -90 → 0 bars
```

Faixas (aproximadas):
- `>= -60 dBm` → 4 bars (excelente)
- `-70 a -60` → 3 bars (bom)
- `-80 a -70` → 2 bars (médio)
- `-90 a -80` → 1 bar (fraco)
- `< -90` → 0 bars (não conecta)

---

## Timeouts sugeridos

- **Scan**: 5s default (passar opção para estender)
- **Connect**: 10s max — se não conectou, algo errado
- **Send command**: 3s por característica
- **Firmware chunk**: 500ms entre chunks

---

## Debugging

### Logs
No Android:
```bash
adb logcat | grep "BleManager\|BleClient"
```

No iOS Simulator: BLE **não funciona no Simulator**. Use device físico.

### Ferramentas externas
- **nRF Connect** (Android/iOS) — app gratuito pra inspecionar advertising de devices BLE nearby
- **Bluetooth Explorer** (macOS) — built-in, debug a baixo nível

### Erros comuns

| Erro | Causa provável |
|---|---|
| `BleError: Operation was cancelled` | `disconnect` durante scan — proteger com state |
| `BleError: Device is not connected` | Device sumiu (fora do range). Reconectar. |
| `BleError: Operation In Progress` | Duas operações simultâneas — sleep entre elas |
| Scan retorna 0 devices | Permissões negadas no Android, ou Bluetooth off |
| `Characteristic not found` | UUID errado — confirmar com firmware |

---

## Checklist ao mexer com BLE

- [ ] Testei em device físico (não no Simulator)
- [ ] Testei offline (app não deve crashear)
- [ ] Permissões negadas tratadas (redireciona pra settings)
- [ ] Single-flight scan preservado
- [ ] Sleeps entre operações mantidos
- [ ] Cleanup ao unmount (disconnect + cancel listeners)
- [ ] Erros mostrados ao user com Toast (não só console)
