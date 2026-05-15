# BLE Epoch Sync — Sincronização de RTC do Sensor

## Visão Geral

A cada conexão BLE com um sensor, o edge service verifica se o RTC do firmware precisa ser atualizado. Se o último sync daquele MAC tiver mais de **7 dias**, o epoch Unix atual é escrito via GATT antes de disparar a coleta de dados.

A sincronização é **não-crítica**: qualquer falha no write é logada e ignorada — a sessão de coleta de dados continua normalmente.

Não há `saveDataToFlash` — o epoch fica em RAM do firmware. Isso é intencional: gravar em flash causaria restart + desconexão do device, interrompendo a coleta. O RTC do STM32WBA65 mantém o valor enquanto houver energia; a próxima conexão semanal atualiza novamente.

---

## Mecanismo GATT

O firmware expõe parâmetros de configuração via dois handles de uso geral:

| Handle | Nome GATT | Direção | Descrição |
|--------|-----------|---------|-----------|
| `48` | `CONFIG/ADDRESS` | write | ID do parâmetro a selecionar |
| `51` | `CONFIG/VALUE` | write | Valor a escrever no parâmetro selecionado |

Para atualizar o epoch, são feitos **dois writes sequenciais**:

```
write(handle=48, value=uint32_LE(2002))        # seleciona PARAM_EPOCH_TIME
write(handle=51, value=uint32_LE(unix_epoch))  # escreve o timestamp atual
```

O parâmetro ID `2002` corresponde a `epochTime` na `paramEntryTable` do firmware (mesmo valor usado pelo `ConnectDeviceFastService` da `api-synchrone`).

### Por que não há SAVE_FLASH

O `ConnectDeviceFastService` (provisionamento via app mobile) escreve `SAVE_FLASH (param 20000)` ao final para persistir todos os parâmetros em flash. Esse write causa **restart do firmware + desconexão BLE imediata**.

No edge service, a conexão ainda está em uso para coletar dados pendentes. Por isso `SAVE_FLASH` **não é chamado**. O epoch fica na RAM do dispositivo e é perdido apenas se o sensor perder energia — situação em que a próxima conexão semanal o restaura.

---

## Posição no Fluxo GATT

O sync acontece dentro de `gatt_setup()`, após autenticação e antes da verificação de slots:

```
1. pair_device()        — Just Works BLE pairing
2. read SN             — lê UID do STM32 (12 bytes)
   calculate password  — TEA com chaves privadas do firmware
   write PASSWORD(54)  — autentica a sessão
2a. [EPOCH SYNC]       — write ADDRESS(48)=2002, write VALUE(51)=epoch  ← aqui
3. read SLOT0/SLOT1    — verifica se outro gateway já está conectado
4. write TYPE(44)      — reserva slot para este gateway
5. write CCCD(59)      — habilita BLE indications de dados
6. write REQUEST(65)   — dispara TX dos frames armazenados (0x01)
```

---

## Controle de Frequência

O `ConnectionManager` mantém um estado por MAC (`_epoch_sync_state`) carregado e salvo em:

```
/opt/sync_reading/epoch_sync.json
```

Formato do arquivo:
```json
{
  "AA:BB:CC:DD:EE:FF": 1747123456.7,
  "11:22:33:44:55:66": 1747098000.1
}
```

Cada entrada é o timestamp Unix (float, segundos) do último sync bem-sucedido daquele MAC.

**Intervalo mínimo**: `7 * 24 * 3600 = 604800 segundos` (7 dias).

A gravação usa rename atômico (`os.replace`) para evitar corrupção em caso de queda do processo durante a escrita.

---

## Comportamento em Casos de Borda

| Situação | Comportamento |
|----------|---------------|
| Falha no write GATT (timeout, NACK) | Logado como `WARN gatt_epoch_sync_error`; coleta de dados prossegue |
| Arquivo `epoch_sync.json` ausente ou corrompido | Inicializa vazio — todos os sensores recebem sync na primeira conexão |
| Sensor reconecta antes de 7 dias | Sync não é feito; `gatt_setup` chamado normalmente sem `sync_epoch=True` |
| Restart do edge service | Estado recarregado do arquivo; janela de 7 dias respeitada |

---

## Logs

| Event key | Level | Quando |
|-----------|-------|--------|
| `gatt_epoch_synced` | INFO | Epoch escrito com sucesso — inclui `epoch_sec` |
| `gatt_epoch_sync_error` | WARN | Falha no write GATT — inclui `error` |
| `epoch_sync_save_error` | WARN | Falha ao gravar `epoch_sync.json` — inclui `error` |

---

## Arquivos Modificados

| Arquivo | Mudança |
|---------|---------|
| `lib/gatt_session.py` | Novos handles `HANDLE_CONFIG_ADDRESS=48`, `HANDLE_CONFIG_VALUE=51`, `PARAM_EPOCH_TIME=2002`; parâmetro `sync_epoch` em `gatt_setup()` |
| `lib/connection_manager.py` | Métodos `_needs_epoch_sync`, `_mark_epoch_synced`, `_load_epoch_sync_file`; parâmetro `epoch_sync_path` |
| `app.py` | Constante `EPOCH_SYNC`; passagem de `epoch_sync_path` ao `connection_manager_loop` |
