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


def flush_spool_one_by_one(
    pending_path: str,
    sending_path: str,
    target_url: str,
    post_json_fn,
    api_key: str,
    api_key_header: str,
    timeout: int,
    spool_max_bytes: int,
    jlog_fn,
    service_name: str,
) -> int:
    """
    Fluxo:
    - pending -> sending (rename atômico)
    - envia 1 item por vez para a API
    - item enviado com sucesso é considerado concluído e não volta ao spool
    - se falhar no meio, reempilha apenas:
        * item atual que falhou
        * itens restantes ainda não processados
    """

    sending = claim_spool(pending_path, sending_path)
    if not sending:
        return 0

    sent_total = 0

    try:
        with open(sending, "r", encoding="utf-8") as f:
            while True:
                line = f.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    item = json.loads(line)
                except Exception:
                    jlog_fn(
                        service_name,
                        "WARN",
                        "spool_invalid_json_line",
                        "Linha inválida no spool; descartando linha",
                        raw_line_preview=line[:500],
                    )
                    continue

                try:
                    # ENVIO SEMPRE INDIVIDUAL
                    post_json_fn(
                        url=target_url,
                        payload=item,
                        api_key=api_key,
                        api_key_header=api_key_header,
                        timeout=timeout,
                    )
                    sent_total += 1

                except Exception as e:
                    remaining: List[Dict[str, Any]] = [item]

                    # lê o restante ainda não processado e reempilha
                    for rest_line in f:
                        rest_line = rest_line.strip()
                        if not rest_line:
                            continue
                        try:
                            remaining.append(json.loads(rest_line))
                        except Exception:
                            continue

                    if spool_max_bytes > 0 and spool_size_bytes(pending_path) >= spool_max_bytes:
                        jlog_fn(
                            service_name,
                            "WARN",
                            "spool_limit_reached",
                            "Spool atingiu limite; descartando eventos para proteger storage",
                            spool_max_bytes=spool_max_bytes,
                            dropped=len(remaining),
                        )
                        remaining = []

                    if remaining:
                        append_many(pending_path, remaining)

                    try:
                        os.remove(sending)
                    except Exception:
                        pass

                    jlog_fn(
                        service_name,
                        "WARN",
                        "spool_flush_failed",
                        "Falha ao reenviar spool item a item",
                        error=str(e),
                        sent_before_failure=sent_total,
                        requeued=len(remaining),
                    )
                    return sent_total

        try:
            os.remove(sending)
        except Exception:
            pass

        return sent_total

    except Exception as e:
        jlog_fn(
            service_name,
            "WARN",
            "spool_processing_error",
            "Erro processando arquivo de spool",
            error=str(e),
        )

        try:
            if os.path.exists(sending):
                # fallback defensivo: devolve todo o arquivo para pending
                remaining = list(iter_jsonl(sending))
                if remaining:
                    append_many(pending_path, remaining)
                os.remove(sending)
        except Exception:
            pass

        return sent_total