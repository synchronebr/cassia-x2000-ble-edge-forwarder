import json
import urllib.request
from typing import Any, Dict, List, Tuple

def post_json(url: str, payload: Dict[str, Any], api_key: str = "", api_key_header: str = "X-API-Key", timeout: int = 10) -> Tuple[int, bytes]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers[api_key_header] = api_key

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()

def post_batch(url: str, items: List[Dict[str, Any]], sent_at_iso: str, api_key: str = "", api_key_header: str = "X-API-Key", timeout: int = 10) -> Tuple[int, bytes]:
    payload = {"sentAt": sent_at_iso, "items": items}
    return post_json(url, payload, api_key=api_key, api_key_header=api_key_header, timeout=timeout)