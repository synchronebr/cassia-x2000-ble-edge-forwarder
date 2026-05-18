import socket
from urllib.parse import urlparse


def check_internet(url: str, timeout: float = 5.0) -> bool:
    """
    Verifica se o host do cloud_url é alcançável via TCP.
    Não envia dados — apenas abre e fecha a conexão.
    """
    try:
        p = urlparse(url)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False
