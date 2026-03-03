import http.client
import random
import time
from typing import Iterator
from urllib.parse import urlparse

def sse_lines(url, read_timeout=60, should_stop=None):
    # type: (str, int, any) -> Iterator[str]
    """
    Lê SSE linha-a-linha (stdlib).
    Retorna linhas já decodadas.
    """
    if should_stop is None:
        should_stop = lambda: False
    
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

        while True:
            if should_stop():
                break
            line = resp.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="ignore").rstrip("\r\n")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def backoff_sleep(attempt, base=1.0, cap=30.0):
    # type: (int, float, float) -> None
    t = min(cap, base * (2 ** attempt))
    t = t * (0.7 + random.random() * 0.6)  # jitter +-30%
    time.sleep(t)
