import json
import urllib.request
from typing import Any, Dict, List, Tuple

def post_json(url, payload, api_key="", api_key_header="X-API-Key", timeout=10):
    # type: (str, Dict[str, Any], str, str, int) -> Tuple[int, bytes]
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers[api_key_header] = api_key

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()

def post_batch(url, items, sent_at_iso, api_key="", api_key_header="X-API-Key", timeout=10):
    # type: (str, List[Dict[str, Any]], str, str, str, int) -> Tuple[int, bytes]
    payload = {"sentAt": sent_at_iso, "items": items}
    return post_json(url, payload, api_key=api_key, api_key_header=api_key_header, timeout=timeout)
