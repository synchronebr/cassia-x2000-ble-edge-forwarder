#!/usr/bin/env python3
"""
Cliente HTTP para a REST API do Cassia X2000.
Sem dependências externas — usa apenas urllib/http.client da stdlib.

Inclui semáforo global limitando chamadas concorrentes ao gateway e
detecção de respostas "gateway busy" (HTTP 503 / body "busy"/"resource")
com backoff aleatório antes de liberar o slot do semáforo.
"""
import http.client
import json as _json
import random
import threading
import time
from urllib.parse import urlparse

# Limita chamadas HTTP simultâneas ao gateway Cassia. Inicializado com 6 —
# pode ser ajustado via set_gateway_concurrency() no boot.
_SEM = threading.Semaphore(6)

_BUSY_LOCK = threading.Lock()
_busy_responses = 0


def set_gateway_concurrency(n: int):
    """Reinicializa o semáforo global com novo limite. Chamar uma vez no boot."""
    global _SEM
    _SEM = threading.Semaphore(max(1, int(n)))


def get_busy_responses() -> int:
    """Total de respostas 'gateway busy' observadas desde o boot."""
    with _BUSY_LOCK:
        return _busy_responses


def _record_busy():
    global _busy_responses
    with _BUSY_LOCK:
        _busy_responses += 1


def _is_busy_response(status: int, body: str) -> bool:
    if status == 503:
        return True
    lower = body.lower()
    return "busy" in lower or "resource" in lower or "try later" in lower


def _request(method: str, url: str, timeout: int = 5, body: dict = None):
    """Executa uma requisição HTTP e retorna (status_code, body_str).

    Serializado pelo semáforo global — o número de chamadas simultâneas ao
    gateway é limitado para evitar saturar a fila HTTP interna do Cassia.

    Se a resposta indicar sobrecarga (503 ou body com 'busy'/'resource'),
    dorme com jitter ANTES de liberar o slot do semáforo. Isso garante que
    todas as threads recuem juntas durante o pico de pressão.
    """
    p = urlparse(url)
    host = p.hostname
    if not host:
        raise RuntimeError("URL inválida (sem host): %s" % url)
    port = p.port or (443 if p.scheme == "https" else 80)
    path = p.path + ("?" + p.query if p.query else "")

    Conn = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection

    with _SEM:
        conn = Conn(host, port, timeout=timeout)
        try:
            if body is not None:
                encoded = _json.dumps(body).encode("utf-8")
                headers = {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(encoded)),
                }
                conn.request(method, path, body=encoded, headers=headers)
            else:
                conn.request(method, path, headers={"Content-Length": "0"})
            resp = conn.getresponse()
            status = resp.status
            body_str = resp.read().decode("utf-8", errors="ignore")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if _is_busy_response(status, body_str):
            _record_busy()
            time.sleep(random.uniform(0.5, 2.0))

        return status, body_str


def connect_device(gateway_api: str, mac: str, chip: int, timeout: int = 5, addr_type: str = "random"):
    """
    POST /gap/nodes/{mac}/connection?chip={chip}&type={addr_type}

    addr_type: "random" (padrão — STM32WBA65 usa endereço random)
               "public" (endereço MAC fixo gravado no chip)

    Retorna (status_code, body).
    """
    url = "{}/gap/nodes/{}/connection?chip={}&type={}".format(
        gateway_api.rstrip("/"), mac, chip, addr_type
    )
    return _request("POST", url, timeout=timeout)


def disconnect_device(gateway_api: str, mac: str, timeout: int = 5):
    """
    DELETE /gap/nodes/{mac}/connection
    Retorna (status_code, body).
    """
    url = "{}/gap/nodes/{}/connection".format(gateway_api.rstrip("/"), mac)
    return _request("DELETE", url, timeout=timeout)


def get_connected_nodes(gateway_api: str, timeout: int = 5) -> list:
    """
    GET /gap/nodes?connection_state=connected

    Retorna lista de dicts com {mac, chip_id} para todos os devices
    atualmente conectados ao gateway.

    Response da API (campo chipId é string "0" ou "1"):
      {"nodes": [{"bdaddrs": {"bdaddr": "MAC", ...}, "chipId": "0", ...}]}

    Lança RuntimeError se o gateway retornar erro HTTP.
    """
    url = "{}/gap/nodes?connection_state=connected".format(gateway_api.rstrip("/"))
    status, body = _request("GET", url, timeout=timeout)
    if status >= 400:
        raise RuntimeError("GET connected nodes HTTP {}: {}".format(status, body[:120]))
    try:
        data = _json.loads(body)
    except Exception:
        return []
    result = []
    for node in data.get("nodes", []):
        bdaddrs = node.get("bdaddrs", {})
        # bdaddrs pode ser dict ou lista — API local retorna dict
        if isinstance(bdaddrs, list):
            bdaddrs = bdaddrs[0] if bdaddrs else {}
        mac = bdaddrs.get("bdaddr", "")
        if not mac:
            mac = node.get("id", "")
        if not mac:
            continue
        try:
            chip_id = int(node.get("chipId", 0))
        except (ValueError, TypeError):
            chip_id = 0
        result.append({"mac": mac.upper(), "chip_id": chip_id})
    return result


def pair_device(gateway_api: str, mac: str, addr_type: str = "public", timeout: int = 15):
    """
    POST /management/nodes/{mac}/pair/
    Inicia pairing BLE (Just Works) após connect_device().
    Necessário antes de escrever em características com segurança exigida.

    iocapability=NoInputNoOutput → pairing Just Works automático (sem passkey).
    bond=0 → pair sem persistir chaves (one-shot).
    timeout no body = timeout BLE em ms; HTTP timeout deve ser maior.

    Retorna (status_code, body).
    """
    url = "{}/management/nodes/{}/pair/".format(gateway_api.rstrip("/"), mac)
    body = {
        "type": addr_type,
        "iocapability": "NoInputNoOutput",
        "timeout": 10000,
        "bond": 0,
    }
    return _request("POST", url, timeout=timeout, body=body)


def set_phy(gateway_api: str, mac: str, tx_phy: int = 2, rx_phy: int = 2, timeout: int = 5):
    """
    POST /gap/nodes/{mac}/phy?tx_phy={tx_phy}&rx_phy={rx_phy}
    Solicita upgrade de PHY após conexão estabelecida.
    Retorna (status_code, body).
    """
    url = "{}/gap/nodes/{}/phy?tx_phy={}&rx_phy={}".format(
        gateway_api.rstrip("/"), mac, tx_phy, rx_phy
    )
    return _request("POST", url, timeout=timeout)


def read_handle(gateway_api: str, mac: str, handle: int, timeout: int = 5):
    """
    GET /gatt/nodes/{mac}/handle/{handle}/value
    Retorna (status_code, body).
    """
    url = "{}/gatt/nodes/{}/handle/{}/value".format(
        gateway_api.rstrip("/"), mac, handle
    )
    return _request("GET", url, timeout=timeout)


def write_handle(gateway_api: str, mac: str, handle: int, value_hex: str, timeout: int = 5):
    """
    GET /gatt/nodes/{mac}/handle/{handle}/value/{value_hex}
    O Cassia usa GET (não POST) para escrita — valor na path distingue read de write.
    Retorna (status_code, body).
    """
    url = "{}/gatt/nodes/{}/handle/{}/value/{}".format(
        gateway_api.rstrip("/"), mac, handle, value_hex
    )
    return _request("GET", url, timeout=timeout)
