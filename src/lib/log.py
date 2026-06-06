import json
from datetime import datetime, timezone

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def jlog(service: str, level: str, event_type: str, message: str, **meta) -> None:
    """
    Log JSONL em stdout. Não coloque segredos em meta.
    """
    entry = {
        "ts": utc_now_iso(),
        "level": level,
        "service": service,
        "event_type": event_type,
        "message": message,
        "meta": meta or {},
    }
    print(json.dumps(entry, ensure_ascii=False), flush=True)