import json
import os
from typing import Any, Dict, Iterable, Iterator, List

def append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def append_many(path: str, items: Iterable[Dict[str, Any]]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

def iter_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue

def spool_size_bytes(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0

def claim_spool(pending_path: str, sending_path: str):
    if not os.path.exists(pending_path):
        return None
    try:
        os.replace(pending_path, sending_path)  # atomic rename
        return sending_path
    except Exception:
        return None

def flush_spool_streaming(
    pending_path: str,
    sending_path: str,
    batch_url: str,
    post_batch_fn,
    api_key: str,
    api_key_header: str,
    timeout: int,
    batch_size: int,
    sent_at_iso_fn,
    spool_max_bytes: int,
    jlog_fn,
    service_name: str,
) -> int:
    """
    - pending -> sending (rename atômico)
    - envia lendo streaming
    - se falhar, reempilha o restante (com limite para proteger storage)
    """
    sending = claim_spool(pending_path, sending_path)
    if not sending:
        return 0

    sent_total = 0
    batch = []  # type: List[Dict[str, Any]]

    try:
        for item in iter_jsonl(sending):
            batch.append(item)
            if len(batch) >= batch_size:
                post_batch_fn(batch_url, batch, sent_at_iso_fn(), api_key=api_key, api_key_header=api_key_header, timeout=timeout)
                sent_total += len(batch)
                batch[:] = []

        if batch:
            post_batch_fn(batch_url, batch, sent_at_iso_fn(), api_key=api_key, api_key_header=api_key_header, timeout=timeout)
            sent_total += len(batch)
            batch[:] = []

        os.remove(sending)
        return sent_total

    except Exception as e:
        remaining = []  # type: List[Dict[str, Any]]
        if batch:
            remaining.extend(batch)

        try:
            for item in iter_jsonl(sending):
                remaining.append(item)
        except Exception:
            pass

        if spool_max_bytes > 0 and spool_size_bytes(pending_path) >= spool_max_bytes:
            jlog_fn(service_name, "WARN", "spool_limit_reached",
                    "Spool atingiu limite; descartando eventos para proteger storage",
                    spool_max_bytes=spool_max_bytes, dropped=len(remaining))
            remaining = []

        if remaining:
            append_many(pending_path, remaining)

        try:
            os.remove(sending)
        except Exception:
            pass

        jlog_fn(service_name, "WARN", "spool_flush_failed", "Falha ao reenviar spool",
                error=str(e), requeued=len(remaining))
        return 0