# Internet Connectivity Check — Estratégia

## Contexto

O edge forwarder roda dentro do Cassia X2000 como um container. O gateway tem dois planos de rede:

- **Rede local (LAN):** acesso ao gateway BLE via SSE (`gateway_api_base`). Sempre disponível enquanto o container estiver rodando.
- **Internet (WAN):** acesso ao backend Go (`cloud_url`). Pode cair sem aviso.

O problema: sem internet, o SSE reader continua consumindo pacotes BLE, o assembler os processa, e o spool cresce — mas nenhum dado sai para o backend. Além do desperdício de CPU/memória, os dados ficam represados em disco sem utilidade.

**Objetivo:** pausar a captura BLE completamente quando a internet não estiver disponível, e retomar automaticamente quando voltar.

---

## Endpoint de Verificação

### Backend escolhido: `go-sensors-synchroone`

O backend Go (`go-sensors-synchroone`) expõe um endpoint de health sem autenticação:

```
GET {cloud_url}/health
```

**Resposta:**
```json
{ "ok": true }
```
**HTTP status:** `200 OK`

> Este endpoint é o mais adequado porque:
> 1. Está no mesmo host/porta que o `POST /sensors/cassia` — valida exatamente o caminho de rede que importa.
> 2. Não exige API Key — sem risco de falso negativo por token inválido.
> 3. É ultraleve — sem lógica de negócio, sem banco de dados.
> 4. Já existe no código (`/cmd/api/main.go`).

### Alternativa: socket TCP (sem HTTP)

Se preferir algo ainda mais leve, um socket TCP puro na porta do `cloud_url` é suficiente:

```python
import socket
from urllib.parse import urlparse

def check_internet(url, timeout=5.0):
    p = urlparse(url)
    host = p.hostname
    port = p.port or (443 if p.scheme == "https" else 80)
    with socket.create_connection((host, port), timeout=timeout):
        return True
```

Neste caso, o check não valida que o servidor Go está respondendo HTTP — só que o host é alcançável via TCP. Para o propósito de "tenho internet?", é suficiente.

**Recomendação:** usar socket TCP, pois é mais rápido (não precisa de handshake HTTP/TLS de conteúdo) e não gera log no backend.

---

## Estrutura de Implementação

### Novo módulo: `lib/connectivity.py`

```python
import socket
from urllib.parse import urlparse

def check_internet(url: str, timeout: float = 5.0) -> bool:
    """
    Verifica se o host do cloud_url está alcançável via TCP.
    Retorna True se conectou, False em qualquer falha.
    """
    try:
        p = urlparse(url)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False
```

Sem dependências externas. Sem HTTP. Sem efeitos colaterais no backend.

---

## Fluxo Completo

```
┌─────────────────────────────────────────────────────────────────┐
│                         BOOT                                    │
│   1. Carrega config                                             │
│   2. Inicia connectivity_monitor_loop (thread daemon)           │
│   3. internet_ok = threading.Event() → inicia NOT SET           │
│   4. Monitor faz 1ª verificação imediatamente                   │
│   5. Se OK → seta internet_ok → main loop prossegue            │
│   6. Se não OK → main loop aguarda (não inicia SSE)             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│             connectivity_monitor_loop  (thread)                 │
│                                                                 │
│   loop:                                                         │
│     ok = check_internet(cloud_url, timeout)                     │
│     se ok:   internet_ok.set()                                  │
│     se !ok:  internet_ok.clear()                                │
│     se mudou de estado: loga internet_ok / internet_lost        │
│     dorme connectivity_check_interval_seconds                   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                      main loop (thread principal)               │
│                                                                 │
│   while not STOP:                                               │
│                                                                 │
│     ┌─ internet_ok não setado? ─────────────────────────────┐  │
│     │   loga no_internet_pause                               │  │
│     │   aguarda internet_ok.wait(timeout=5s)                 │  │
│     │   volta ao início do while                             │  │
│     └────────────────────────────────────────────────────────┘  │
│                                                                 │
│     loga gatt_connecting                                        │
│     conecta SSE do gateway BLE                                  │
│                                                                 │
│     for pacote in sse_events(..., should_stop=should_stop_sse): │
│       processa pacote → packet_queue                            │
│                                                                 │
│     ← SSE encerrou (normal ou por internet_lost)               │
│                                                                 │
│     ┌─ internet caiu? ──────────────────────────────────────┐  │
│     │   volta ao início do while sem loga erro              │  │
│     └────────────────────────────────────────────────────────┘  │
│     ┌─ outro motivo? ───────────────────────────────────────┐  │
│     │   loga gatt_error + backoff_sleep                      │  │
│     └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### `should_stop_sse()` atualizado

```python
def should_stop_sse():
    return bool(STOP) or not internet_ok.is_set()
```

Quando a internet cai durante uma sessão SSE ativa, o monitor limpa o evento `internet_ok`. Na próxima iteração do `readline()` do SSE, o `should_stop_sse()` retorna `True`, o gerador para limpo, e o main loop detecta que foi internet loss — não loga como erro.

---

## Diagrama de Estados

```
                  ┌──────────────┐
           boot   │              │  check ok
        ─────────►│  AGUARDANDO  │──────────────────────────┐
                  │  INTERNET    │                           │
                  └──────┬───────┘                           │
                         │ check ok                          ▼
                         │                        ┌──────────────────┐
                         │              ┌─────────►  SSE CONECTANDO  │
                         │              │          └────────┬─────────┘
                         │              │ retry             │ conectou
                         │              │                   ▼
                         │              │          ┌──────────────────┐
                         │              │          │  SSE ATIVO       │
                         │              │          │  capturando BLE  │
                         │              │          └────────┬─────────┘
                         │              │                   │
                         │              │    ┌──────────────┴──────────────┐
                         │              │    │                             │
                         │              │    ▼ internet cai               ▼ falha SSE
                         │    ┌─────────┴──────────┐           ┌──────────────────────┐
                         │    │  AGUARDANDO        │           │  BACKOFF + RETRY     │
                         └────│  INTERNET          │           │  (gatt_error)        │
                              └────────────────────┘           └──────────────────────┘
```

---

## Configuração

Nova chave no `config.json`:

| Campo | Padrão | Descrição |
|-------|--------|-----------|
| `connectivity_check_interval_seconds` | `15` | Intervalo entre verificações de internet |
| `connectivity_check_timeout_seconds` | `5` | Timeout do socket connect |

O campo `timeout_seconds` existente pode ser reutilizado como `connectivity_check_timeout_seconds` se preferir manter o config simples.

---

## Eventos de Log Produzidos

| `event` | Nível | Quando |
|---------|-------|--------|
| `internet_ok` | INFO | Internet voltou (mudança de estado) |
| `internet_lost` | WARN | Internet caiu (mudança de estado) |
| `no_internet_pause` | WARN | Main loop aguardando (1x por ciclo de espera) |

Mudanças de estado são logadas apenas quando mudam — o monitor não loga a cada check se o estado for o mesmo.

---

## O que NÃO muda

- O `sender_loop` continua rodando normalmente. Se por algum motivo o spool tiver dados pendentes de uma sessão anterior, ele continua tentando enviar quando a internet estiver disponível.
- O `spool_writer` continua rodando — mas como o `assembler` não receberá novos pacotes (SSE parado), na prática não há novos eventos para escrever.
- O `assembler_loop` continua rodando, mas a `packet_queue` ficará vazia enquanto o SSE estiver pausado.
- Nenhuma mudança nas threads existentes. O monitor é uma thread adicional.

---

## Arquivos a Criar/Modificar

| Arquivo | Ação |
|---------|------|
| `lib/connectivity.py` | Criar — função `check_internet(url, timeout)` |
| `app.py` | Modificar — adicionar monitor thread, atualizar main loop e `should_stop_sse` |
| `config.json` | Modificar — adicionar `connectivity_check_interval_seconds` |

---

## Referências do Backend

- Endpoint health: `GET {cloud_url}/health` → `200 { ok: true }`
- Implementação: `go-sensors-synchroone/cmd/api/main.go`
- Middleware API Key (não usado no health): `go-sensors-synchroone/internal/shared/infra/http/middleware.go`
  - Header esperado: `key: <APP_KEY>`
- Endpoint de ingestão: `POST {cloud_url}/sensors/cassia`
  - Autenticação: header `key: <APP_KEY>`
