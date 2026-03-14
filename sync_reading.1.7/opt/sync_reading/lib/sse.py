#!/usr/bin/env python3
import http.client
import random
import time
from typing import Iterator
from urllib.parse import urlparse


def sse_events(url: str, read_timeout: int = 60, should_stop=lambda: False) -> Iterator[str]:
    """
    Lê SSE por evento completo.

    Acumula múltiplas linhas 'data:' até encontrar uma linha vazia,
    conforme o framing padrão do SSE.

    Retorna apenas o conteúdo concatenado do campo data.
    """
    p = urlparse(url)
    host = p.hostname
    if not host:
        raise RuntimeError("SSE URL inválida (sem host)")

    port = p.port or (443 if p.scheme == "https" else 80)
    path = p.path + (("?" + p.query) if p.query else "")

    Conn = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = Conn(host, port, timeout=read_timeout)

    try:
        conn.putrequest("GET", path)
        conn.putheader("Accept", "text/event-stream")
        conn.putheader("Cache-Control", "no-cache")
        conn.endheaders()

        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError("SSE status {}".format(resp.status))

        data_parts = []

        while True:
            if should_stop():
                break

            line = resp.readline()
            if not line:
                # conexão fechou
                break

            line = line.decode("utf-8", errors="ignore").rstrip("\r\n")

            # fim de um evento SSE
            if line == "":
                if data_parts:
                    yield "\n".join(data_parts)
                    data_parts = []
                continue

            if line.startswith("data:"):
                data_parts.append(line[len("data:"):].lstrip())

            # ignoramos outros campos SSE como event:, id:, retry:, etc.

        # flush final, se a conexão fechar sem linha em branco
        if data_parts:
            yield "\n".join(data_parts)

    finally:
        try:
            conn.close()
        except Exception:
            pass


def backoff_sleep(attempt: int, base: float = 1.0, cap: float = 30.0) -> None:
    t = min(cap, base * (2 ** attempt))
    t = t * (0.7 + random.random() * 0.6)  # jitter +-30%
    time.sleep(t)