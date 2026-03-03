import json
import os
from typing import Any, Dict, Tuple

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_cfg(app_name: str, base_dir: str, env_cfg_path: str | None = None) -> Tuple[Dict[str, Any], str]:
    """
    Prioridade:
      1) env_cfg_path (ex.: APP_CONFIG)
      2) /root/config/<app_name>/config.json  (se você usar UI/AC no futuro)
      3) <base_dir>/config.json
    """
    candidates = []
    if env_cfg_path:
        candidates.append(env_cfg_path)

    candidates.append(f"/root/config/{app_name}/config.json")
    candidates.append(os.path.join(base_dir, "config.json"))

    for p in candidates:
        if p and os.path.exists(p):
            return load_json(p), p

    raise FileNotFoundError(f"Nenhum config.json encontrado. Tentados: {candidates}")

def get_str(cfg: Dict[str, Any], key: str, default: str = "") -> str:
    v = cfg.get(key, default)
    return "" if v is None else str(v)

def get_int(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except Exception:
        return default