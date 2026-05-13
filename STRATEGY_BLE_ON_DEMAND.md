# Estratégia BLE On-Demand — Synchroone / Cassia X2000

> **Audiência:** Desenvolvedor do firmware (STM32WBA65) e desenvolvedor do serviço de borda (Python).  
> **Contexto:** Reformulação da arquitetura de transmissão BLE com base na recomendação do suporte Cassia (abril/2026).

---

## 1. Diagnóstico da Situação Atual

### 1.1 Firmware (STM32WBA65)

| Parâmetro | Valor atual |
|-----------|-------------|
| PHY | **Coded PHY (S=8 = 125 kbps efetivos)** |
| MTU máximo | 247 bytes → payload GATT 244 bytes por pacote |
| Intervalo de conexão (TX) | 30 ms (FAST) |
| Intervalo de conexão (idle) | 200 ms (SLOW) |
| Conexões simultâneas suportadas | `CFG_BLE_NUM_LINK = 2` |
| Advertising (normal) | 50–62,5 ms (`80–100 × 0,625 ms`) |
| Advertising (low-power) | 1000–2500 ms (`1600–4000 × 0,625 ms`) |
| Payload de advertising atual | Apenas nome `"Sync"` (6 bytes) + UID do STM32 (Service Data) |
| ODR do IIS3DWB | 26.667 Hz |
| Buffer de amostras | 26.700 amostras × 6 bytes = **~160 KB por janela de 1 s** |
| Ciclo de vida da conexão | **Persistente** — sensor permanece conectado entre transmissões |

### 1.2 Volume de Dados × Throughput Real

Com **Coded PHY S=8 + intervalo 30 ms + payload 244 bytes**:

```
Throughput efetivo ≈ 244 bytes / 30 ms = ~8 KB/s
Janela 1 s (160 KB)  → ~20 segundos de transmissão
Janela 100 ms (16 KB) → ~2 segundos de transmissão
```

O Coded PHY foi projetado para alcance máximo, não para throughput. Para sensores a curta distância (< 10 m do gateway), é contra-indicado.

**Com 2M PHY + intervalo 7,5 ms (viável no modelo on-demand com 1 conexão por chip):**

```
Throughput efetivo ≈ 244 bytes / 7,5 ms = ~32 KB/s
Janela 200 ms (32 KB) → ~1 segundo de transmissão
Janela 1 s (160 KB)   → ~5 segundos de transmissão
```

### 1.3 Problema Central

O suporte Cassia indica que com **15 conexões simultâneas por chip**, o intervalo mínimo viável é 100 ms — limitando o throughput a ≈ 2,4 KB/s por sensor. Com 50 KB de dados, isso leva a **mais de 20 segundos por sensor**.

No modelo atual (conexão persistente + Coded PHY + janela grande), o gateway fica sobrecarregado e os sensores competem por slots de rádio de forma ineficiente.

---

## 2. Nova Arquitetura: On-Demand com Advertising Sinalizado

### 2.1 Princípio

```
ANTES:  Sensor conectado 24/7 → gateway detecta notificações → encaminha dados
DEPOIS: Sensor anuncia "tenho dados" no advertising → gateway conecta → coleta → desconecta
```

Benefícios diretos:
- Gateway mantém **máximo 2 conexões ativas simultaneamente** (1 por chip BLE)
- Intervalo de conexão pode ser **7,5–15 ms** sem conflito (baixa concorrência)
- PHY 2M aplica-se a sensores com RSSI > −75 dBm → 8× mais throughput que Coded PHY
- Sensor volta ao modo **low-power** imediatamente após TX

### 2.2 Fluxo de Ciclo Completo

```
[Sensor]                              [Cassia X2000]           [Python Edge Service]

   |-- Acorda (timer hibernação)           |                          |
   |-- Lê IIS3DWB (janela configurável)    |                          |
   |-- Seta data_pending=1 no adv_data     |                          |
   |-- Advertising com intervalo normal    |                          |
   |   (50–62,5 ms)                        |                          |
   |                                       |<-- SSE /gap/nodes -------|
   |                                       |    (scan contínuo)       |
   |<-- Connect (chip 0 ou chip 1) --------|-- POST /gap/connect -----|
   |-- Negocia 2M PHY                      |                          |
   |-- Negocia intervalo 7,5–15 ms         |                          |
   |-- Negocia MTU 247                     |                          |
   |-- TX: START → TIMESTAMP → dados       |                          |
   |         → END (via Indication)        |                          |
   |                                       |--- SSE GATT events ----->|
   |<-- Disconnect ------------------------|                          |
   |-- Volta a advertising low-power       |                          |
   |   com data_pending=0 (1000–2500 ms)   |                          |
   |-- Entra em hibernação                 |                          |
```

---

## 3. Alterações no Firmware (Entregável para o Desenvolvedor do Device)

### 3.1 Advertising Payload com Flag `data_pending`

#### Estratégia de sinalização: presença/ausência do AD Type 0xFF

O sinal `data_pending` **não é um bit** — é a **presença ou ausência do bloco Manufacturer Specific Data** no advertising packet. Quando o sensor não tem dados, o AD Type `0xFF` está completamente ausente do payload. O filtro Cassia `filter_manufacturer_data=FFFF` só passa eventos onde esse bloco existe, portanto filtra automaticamente todos os sensores idle sem nenhum check client-side.

O advertising packet BLE tem **31 bytes úteis** no payload primário. A estrutura atual já ocupa:
- Flags AD: 3 bytes
- Complete Local Name `"Sync"`: 6 bytes

**Layout quando idle** (sem dados prontos):
```
[Flags 3B][Local Name "Sync" 6B] = 9 bytes  — AD Type 0xFF ausente
```

**Layout quando dados prontos**:
```
[Flags 3B][Local Name "Sync" 6B][Manufacturer Specific 7B] = 16 bytes
```

O bloco Manufacturer Specific Data quando presente:

```
Offset  Tamanho  Campo
------  -------  -----
0       1 byte   Length = 6 (bytes após este campo)
1       1 byte   AD Type = 0xFF (Manufacturer Specific Data)
2       2 bytes  Company ID LE (0xFFFF em desenvolvimento; registrar no Bluetooth SIG para produção)
4       1 byte   seq_num (uint8) — incrementa a cada nova leitura. Muda o payload e
                   destrava o filter_duplicates=0 do Cassia, sinalizando nova leitura disponível.
                   Usado pelo Connection Manager para deduplicação: não conectar se
                   seq_num == último coletado para este MAC.
5       1 byte   status:
                   bit 0 = error_flag    (1 = sensor em falha)
                   bit 1 = low_battery   (1 = bateria crítica)
                   bits 2–7 = reservado (0)
6       1 byte   pending_count (uint8) — leituras acumuladas prontas para coleta.
                   Connection Manager usa para priorizar sensores com mais dados.
```

**Filtro Cassia resultante:**
```
GET /gap/nodes?event=1&filter_name=Sync&filter_manufacturer_data=FFFF&filter_duplicates=0&filter_rssi=-90
```
- Sensores idle (sem AD `0xFF`) → nunca passam no filtro → silêncio total no SSE
- Sensor com dados prontos → AD `0xFF` presente → 1 evento enviado
- Sensor re-anuncia com mesmo payload → duplicata suprimida → silêncio
- Nova leitura (seq_num diferente) → payload mudou → Cassia destrava e envia novo evento

#### Implementação no firmware

Localizar em `STM32_WPAN/App/app_ble.c`:

```c
// Company ID (0xFFFF para desenvolvimento; registrar no Bluetooth SIG para produção)
#define SYNC_COMPANY_ID_L  0xFF
#define SYNC_COMPANY_ID_H  0xFF

// Advertising idle: sem Manufacturer Specific Data
static const uint8_t a_AdvData_Idle[] = {
    0x02, 0x01, 0x06,
    0x05, 0x09, 'S', 'y', 'n', 'c',
};

// Advertising com dados prontos: Manufacturer Specific Data presente
static uint8_t a_AdvData_Ready[] = {
    0x02, 0x01, 0x06,
    0x05, 0x09, 'S', 'y', 'n', 'c',
    // [length=6][type=0xFF][companyID 2B][seq_num][status][pending_count]
    0x06, 0xFF, SYNC_COMPANY_ID_L, SYNC_COMPANY_ID_H,
    0x00,  // [idx 12] seq_num
    0x00,  // [idx 13] status
    0x00,  // [idx 14] pending_count
};

#define ADV_READY_IDX_SEQ     12
#define ADV_READY_IDX_STATUS  13
#define ADV_READY_IDX_PENDING 14

static uint8_t seqNum = 0;

// Chamar antes de aguardar conexão: adiciona AD 0xFF e sinaliza ao gateway
void SyncAdv_SetReady(uint8_t pendingCount, uint8_t status)
{
    a_AdvData_Ready[ADV_READY_IDX_SEQ]     = ++seqNum;
    a_AdvData_Ready[ADV_READY_IDX_STATUS]  = status;
    a_AdvData_Ready[ADV_READY_IDX_PENDING] = pendingCount;
    aci_gap_update_adv_data(sizeof(a_AdvData_Ready), a_AdvData_Ready);
}

// Chamar após TX completo: remove AD 0xFF, sensor some do filtro Cassia
void SyncAdv_SetIdle(void)
{
    aci_gap_update_adv_data(sizeof(a_AdvData_Idle), a_AdvData_Idle);
}
```

**Onde chamar em `synchroone_routine.c`:**

| Momento | Chamada |
|---------|---------|
| Leitura do sensor concluída, antes de aguardar conexão | `SyncAdv_SetReady(1, 0)` |
| TX concluído, em `handleStateDone()` | `SyncAdv_SetIdle()` |

### 3.2 Mudança de PHY

Arquivo: `Core/Inc/app_conf.h`

```c
// ANTES (Coded PHY — manter apenas para longo alcance / pior RSSI):
#define CFG_PHY_PREF_TX   (HCI_TX_PHYS_LE_CODED_PREF)
#define CFG_PHY_PREF_RX   (HCI_RX_PHYS_LE_CODED_PREF)

// DEPOIS (sem preferência — aceita o que o central propuser):
#define CFG_PHY_PREF      (HCI_ALL_PHYS_TX_NO_PREF | HCI_ALL_PHYS_RX_NO_PREF)
#define CFG_PHY_PREF_TX   (HCI_TX_PHYS_LE_1M_PREF | HCI_TX_PHYS_LE_2M_PREF)
#define CFG_PHY_PREF_RX   (HCI_RX_PHYS_LE_1M_PREF | HCI_RX_PHYS_LE_2M_PREF)
```

O gateway Python (via Cassia API) iniciará a negociação de PHY upgrade para 2M após a conexão quando RSSI > −75 dBm.

> **Alternativa conservadora:** manter Coded PHY apenas se houver cenário real de sensores a mais de 20 m do gateway. Para a maioria das instalações industriais (sensor fixo, gateway a até 10 m), 1M PHY é suficiente e 10× mais rápido.

### 3.3 Ciclo de Vida da Conexão — Desconexão Após TX

Atualmente o sensor permanece conectado indefinidamente. No novo modelo:

Em `Synchroone/synchroone_routine.c`, após `TASK_FLOW_CTRL_STATE_WAIT_CONN_INT_SLOW`:

```c
static inline void handleStateDone(void)
{
    UTIL_TIMER_Stop(&taskFlowCtrlTimerBurst);

    // NOVO: solicitar desconexão de todas as sessões ativas
    BLE_SESSION_DisconnectAll();  // implementar: chama aci_gap_terminate() para cada handle

    // Remover Manufacturer Specific Data: sensor some do filtro Cassia automaticamente
    SyncAdv_SetIdle();

    // Retomar advertising low-power para que o gateway saiba que o sensor está vivo
    APP_BLE_Procedure_Gap_Peripheral(PROC_GAP_PERIPH_ADVERTISE_START_LP);
}
```

### 3.4 Advertising de "Dados Prontos" Antes de Aguardar Conexão

Em `handleStateStart()` ou no início da máquina de estado, após leitura dos sensores e **antes** de aguardar conexão:

```c
// pending_count: leituras prontas (normalmente 1, >1 se houver spool no device)
uint8_t pendingCount = GetPendingReadingsCount();

// Adicionar AD Type 0xFF ao advertising — sensor passa a ser visível no filtro Cassia
SyncAdv_SetReady(pendingCount, 0x00);

// Mudar para intervalo de advertising normal (mais rápido para o gateway encontrar)
APP_BLE_Procedure_Gap_Peripheral(PROC_GAP_PERIPH_ADVERTISE_START_FAST);
```

### 3.5 Redução da Janela de Amostragem (Recomendação)

O buffer atual suporta até 26.700 amostras × 6 bytes = 160 KB. Para o cenário com o Cassia:

| `samplingTime_ms` | Amostras | Bytes | TX @ 32 KB/s | TX @ 8 KB/s |
|-------------------|----------|-------|--------------|-------------|
| 100 ms            | 2.667    | 16 KB | 0,5 s        | 2 s         |
| 200 ms            | 5.334    | 32 KB | 1 s          | 4 s         |
| 500 ms            | 13.335   | 80 KB | 2,5 s        | 10 s        |
| 1000 ms           | 26.667   | 160 KB| 5 s          | 20 s        |

**Recomendação inicial: 200 ms** (32 KB). Boa resolução temporal, TX rápida, e cabe em 1 segundo com 2M PHY + intervalo curto.

O parâmetro já é configurável via flash (`iis3dwb.samplingTime_ms`). Basta ajustar o valor padrão em `FLASH_ResetFlashAppDataToDefault()`.

### 3.6 DLE (Data Length Extension)

Garantir que o firmware solicite DLE após conexão:

```c
// Após evento de conexão estabelecida (em app_ble.c, handler de HCI_LE_CONNECTION_COMPLETE):
hci_le_set_data_length(connectionHandle, 251, 2120);
// 251 bytes = PDU máximo BLE 5.x
// 2120 µs = max TX time para 251 bytes em 1M PHY
```

Sem DLE, mesmo com MTU 247, o controlador fragmenta em PDUs de 27 bytes — reduzindo o throughput em até 9×.

---

## 4. Alterações no Serviço de Borda Python

### 4.1 Avaliação: Python vs. Node.js

**Veredicto: manter Python.**

| Critério | Python | Node.js |
|----------|--------|---------|
| Runtime no Cassia X2000 (embedded Linux) | Python 3 geralmente disponível | Node.js não está pré-instalado; adicionar ~50 MB |
| Dependências externas | Zero (stdlib apenas) | npm obrigatório; surface area de segurança |
| SSE + I/O concorrente | Threads adequadas (GIL não é gargalo — 100% I/O bound) | Event loop é elegante mas não necessário aqui |
| Gestão ativa de conexões (novo modelo) | `urllib` + threads resolvem | `fetch` + async/await mais legível, mas não decisivo |
| Risco de migração | Zero — já testado em hardware real | Alto — reescrever tudo, reprovar em hardware |

O gargalo do sistema não é CPU do Python: é throughput BLE e tamanho dos dados. Node.js não resolveria nenhum dos problemas reais.

### 4.2 Nova Arquitetura do Serviço Python

```
Cassia SSE /gap/nodes (scan)
      ↓
[Scan Reader] ──→ scan_queue
                      ↓
               [Connection Manager]
               (AD 0xFF presente = dados prontos,
                gerencia 2 chips,
                evita reconectar mesmo seq_num)
                      ↓
          POST /gap/connect (chip 0 ou chip 1)
                      ↓
[GATT SSE Reader] ──→ packet_queue (pipeline existente, sem mudanças)
                      ↓
               [Assembler → Spool → Cloud]
                      ↓
          POST /gap/disconnect (após TX_DONE detectado)
```

O pipeline existente (packet_queue → Assembler → Spool → Cloud) **não muda**. Apenas o front-end de aquisição muda: de SSE passivo para scan ativo + conexão programática.

### 4.3 Parsing do Advertising Payload

Retornar `None` significa sensor idle ou não-Synchroone. Retornar um dict significa que dados estão prontos — não há campo `data_pending` porque a presença do dict já é esse sinal.

```python
import struct

COMPANY_ID_SYNCHROONE = 0xFFFF  # substituir pelo ID registrado no Bluetooth SIG em produção

def parse_synchroone_adv(adv_data_hex: str) -> dict | None:
    """
    Retorna dict se o sensor tem dados prontos (AD Type 0xFF presente com Company ID Synchroone).
    Retorna None se idle (AD 0xFF ausente) ou não for sensor Synchroone.
    """
    data = bytes.fromhex(adv_data_hex)
    i = 0
    while i < len(data):
        if i + 1 >= len(data):
            break
        length = data[i]
        if length == 0 or i + length >= len(data):
            break
        ad_type = data[i + 1]
        payload = data[i + 2: i + 1 + length]
        if ad_type == 0xFF and len(payload) >= 5:
            company_id = struct.unpack_from('<H', payload, 0)[0]
            if company_id == COMPANY_ID_SYNCHROONE:
                seq_num, status, pending_count = struct.unpack_from('<BBB', payload, 2)
                return {
                    'seq_num':       seq_num,
                    'error_flag':    bool(status & 0x01),
                    'low_battery':   bool(status & 0x02),
                    'pending_count': pending_count,
                }
        i += 1 + length
    return None
```

**Como o Connection Manager usa os campos:**
- `seq_num` — deduplicação: não conectar se `seq_num == último coletado` para este MAC. Com `filter_duplicates=0`, o Cassia só reencaminha o evento quando `seq_num` muda, sinalizando nova leitura.
- `pending_count > 1` — priorização: sensores com mais leituras acumuladas sobem na fila de conexão.
- `error_flag` — logar e conectar normalmente; o firmware transmite o que tiver. Não bloquear coleta por erro.
- `pending_count` após TX_DONE — se o sensor re-anunciar com `seq_num` diferente do coletado, há nova leitura: Connection Manager conecta novamente.

### 4.4 Connection Manager (novo módulo `lib/connection_manager.py`)

Responsabilidades:
- Manter estado de cada chip BLE do Cassia (`chip_0_busy`, `chip_1_busy`)
- Receber eventos de scan da scan_queue (já pré-filtrados pelo Cassia: só chegam sensores com AD `0xFF` presente)
- Descartar se `seq_num == último_coletado[mac]` — mesmo dado já coletado
- Enfileirar na fila de conexão por `pending_count` decrescente (mais dados primeiro), desempate por RSSI
- Disparar `POST /gap/connect?chip=N` quando slot disponível
- Iniciar leitura do GATT SSE para aquela conexão
- Disparar `POST /gap/disconnect` quando assembler sinalizar TX_DONE para aquele device
- **Retry:** se assembly timeout sem TX_DONE, não esperar novo evento SSE (o Cassia suprimiu como duplicata). Usar timer de retry: após `assembly_timeout_seconds + margem`, tentar reconectar diretamente pelo MAC se o sensor ainda estiver anunciando.

**Estado por sensor:**
```python
sensor_state = {
    "AA:BB:CC:DD:EE:FF": {
        "last_seq_collected": 1,   # seq_num do último TX_DONE bem-sucedido
        "last_seq_seen":      2,   # seq_num do último adv recebido
        "chip_slot":          None # chip Cassia alocado (0, 1 ou None)
    }
}
```

**Sinalização de TX_DONE para o Connection Manager:**

O assembler já produz eventos completos com `device` MAC. Adicionar callback no assembler (ou verificar a outbound_queue) para notificar o connection manager quando todos os pacotes de um device foram recebidos. Ao receber TX_DONE: setar `last_seq_collected = last_seq_seen`, liberar `chip_slot`.

### 4.5 PHY Upgrade via Cassia API

Após conexão estabelecida, verificar o RSSI do evento de scan para aquele MAC:

```python
# Se RSSI >= -75 dBm → solicitar 2M PHY
if scan_rssi >= -75:
    cassia_api.set_phy(connection_handle, tx_phy=2, rx_phy=2)
# Se RSSI < -85 dBm → manter Coded PHY ou 1M
```

A Cassia expõe `POST /gap/phy` (verificar versão da API — disponível a partir do firmware Cassia 2.1.x).

---

## 5. Dois Módulos BLE do Cassia em Paralelo

O Cassia X2000 possui **2 chips BLE** internos. Cada chip deve ser usado de forma independente:

```
Chip 0 → conectado ao Sensor A (coletando dados)
Chip 1 → conectado ao Sensor B (coletando dados)
```

No serviço Python, o connection manager mantém dois slots independentes. A Cassia API usa o parâmetro `chip` nas chamadas de conexão:

```
POST /gap/connect?mac=XX:XX:XX:XX:XX:XX&chip=0
POST /gap/connect?mac=YY:YY:YY:YY:YY:YY&chip=1
```

Para o scan de advertising (`/gap/nodes`), o Cassia geralmente unifica os dois chips no mesmo stream SSE. Verificar na documentação Cassia X2000 API se há parâmetro `chip` para filtrar. Se não houver, o Connection Manager determina qual chip usar para a conexão com base nos slots livres.

### Taxa de throughput com 2 chips em paralelo

Com 2M PHY + intervalo 15 ms + 2 chips:
```
Por chip: 244 bytes / 15 ms = ~16 KB/s
Dois chips: ~32 KB/s total
Janela 200 ms (32 KB por sensor):
  - Sensor A: TX em ~2 s
  - Sensor B: TX em ~2 s simultaneamente
  - Throughput sistema: ~1 sensor novo a cada 2 segundos = 30 sensores/minuto
```

---

## 6. Resumo das Tarefas por Área

### 6.1 Firmware (Desenvolvedor do Device — STM32WBA65)

| # | Tarefa | Arquivo | Prioridade |
|---|--------|---------|------------|
| F1 | Criar `a_AdvData_Idle[]` (sem AD `0xFF`) e `a_AdvData_Ready[]` (com AD `0xFF`) | `STM32_WPAN/App/app_ble.c` | Alta |
| F2 | Implementar `SyncAdv_SetReady(pendingCount, status)` e `SyncAdv_SetIdle()` | `STM32_WPAN/App/app_ble.c` | Alta |
| F3 | Chamar `SyncAdv_SetReady(1, 0)` antes de aguardar conexão | `Synchroone/synchroone_routine.c` | Alta |
| F4 | Chamar `SyncAdv_SetIdle()` após TX completo em `handleStateDone()` | `Synchroone/synchroone_routine.c` | Alta |
| F5 | Adicionar `BLE_SESSION_DisconnectAll()` em `handleStateDone()` | `Synchroone/ble_session.c` | Alta |
| F6 | Trocar PHY padrão de Coded para 1M+2M (sem preferência) em `app_conf.h` | `Core/Inc/app_conf.h` | Alta |
| F7 | Solicitar DLE (`hci_le_set_data_length(251, 2120)`) no evento de conexão | `STM32_WPAN/App/app_ble.c` | Média |
| F8 | Reduzir `samplingTime_ms` padrão para 200 ms em `FLASH_ResetFlashAppDataToDefault()` | `Synchroone/flash_appdata.c` | Média |
| F9 | Ao iniciar advertising de "dados prontos", usar intervalo FAST (50 ms); após TX, voltar para LP (1000–2500 ms) | `Synchroone/synchroone_routine.c` | Média |
| F10 | **Registrar Company ID** no Bluetooth SIG ou usar `0xFFFF` durante desenvolvimento | — | Baixa (pode usar 0xFFFF temporariamente) |

### 6.2 Serviço de Borda Python (Desenvolvedor do Edge Service)

| # | Tarefa | Arquivo | Prioridade |
|---|--------|---------|------------|
| P1 | Implementar `parse_synchroone_adv(hex)` — retorna dict (dados prontos) ou None (idle/não-Synchroone) | `lib/adv_parser.py` (novo) | Alta |
| P2 | Implementar `ConnectionManager` com fila de conexão, estado dos 2 chips, dedup por seq_num | `lib/connection_manager.py` (novo) | Alta |
| P3 | Adicionar SSE reader para `/gap/nodes` (scan events) separado do GATT SSE | `lib/scan_reader.py` (novo) | Alta |
| P4 | Integrar ConnectionManager em `app.py`: trocar GATT SSE passivo por scan ativo | `app.py` | Alta |
| P5 | Adicionar chamada de PHY upgrade após conexão se RSSI ≥ −75 dBm | `lib/connection_manager.py` | Média |
| P6 | Corrigir bug em `lib/rssi.py`: `sse_lines` → `sse_events` (inexistente) e conectar ao app | `lib/rssi.py` | Média |
| P7 | Ajustar config.json: `outbound_queue_max` (conflito: arquivo=2000, default código=5000) | `config.json` | Baixa |

---

## 7. Ordem de Implementação Sugerida

```
Sprint 1 (Firmware — base):
  F1, F2, F3, F4, F5  → sensor sinaliza data_pending e desconecta após TX

Sprint 1 (Edge — base):
  P1, P3, P2           → serviço detecta data_pending e gerencia conexões

Sprint 2 (Performance):
  F6, F7, F8           → PHY 2M + DLE + janela menor
  P4, P5               → PHY upgrade via Cassia API

Sprint 3 (Refinamento):
  F9                   → otimização de advertising interval
  P6, P7               → RSSI módulo, config cleanup
  F10                  → registrar Company ID definitivo
```

---

## 8. Riscos e Mitigações

| Risco | Mitigação |
|-------|-----------|
| Cassia API não suporta `chip=N` no connect | Testar; se não suportar, connection manager usa round-robin por MAC sem especificar chip |
| Gateway demora a detectar data_pending (advertising interval muito longo) | Usar ADV_FAST (50 ms) durante janela de "dados prontos"; voltar para LP após TX |
| Sensor perde conexão no meio da TX | Assembler timeout; Cassia suprimiu re-anúncio como duplicata (mesmo seq_num). Connection Manager usa timer de retry ativo para reconectar sem depender do SSE. |
| seq_num overflow (uint8 → wrap em 256) | Considerar uint16 no campo seq_num; ou usar timestamp de 2 bytes (últimos 16 bits do epoch) |
| Dois sensores com mesmos dados transmitindo simultaneamente para um chip | dedup por `device_mac + seq_num` no ConnectionManager |
| PHY 2M não disponível no ambiente (paredes, interferência) | Fallback automático para 1M PHY via BLE handshake — sem código adicional |
| Firmware atual não desconecta → gateway fica bloqueado | Sprint 1 resolve: F5 garante desconexão no `handleStateDone()` |

---

## 9. Referências Técnicas

- **Cassia X2000 REST API**: `/gap/nodes` (scan), `/gap/connect`, `/gap/disconnect`, `/gap/phy`
- **STM32WBA65 BLE Stack**: `aci_gap_update_adv_data()`, `hci_le_set_data_length()`, `aci_gap_terminate()`
- **BLE Spec 5.4**: Manufacturer Specific AD Type = `0xFF`; Company ID registry: bluetooth.com/specifications/assigned-numbers/
- **Cassia support email**: On-demand connections + PHY optimization (abril/2026)
- **Throughput BLE calculado**: [Bluetooth Throughput Calculator — Nordic](https://devzone.nordicsemi.com/nordic/nordic-blog/b/blog/posts/ble-throughput-tutorial)
