import json
import os
from typing import Any, Dict, Tuple, Optional

def load_json(path):
    # type: (str) -> Dict[str, Any]
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_cfg(app_name, base_dir, env_cfg_path=None):
    # type: (str, str, Optional[str]) -> Tuple[Dict[str, Any], str]
    """
    Prioridade:
      1) env_cfg_path (ex.: APP_CONFIG)
      2) /root/config/<app_name>/config.json
      3) <base_dir>/config.json
    """
    candidates = []
    if env_cfg_path:
        candidates.append(env_cfg_path)

    candidates.append("/root/config/{}/config.json".format(app_name))
    candidates.append(os.path.join(base_dir, "config.json"))

    for p in candidates:
        if p and os.path.exists(p):
            return load_json(p), p

    raise FileNotFoundError("Nenhum config.json encontrado. Tentados: {}".format(candidates))

def get_str(cfg, key, default=""):
    # type: (Dict[str, Any], str, str) -> str
    v = cfg.get(key, default)
    return "" if v is None else str(v)

def get_int(cfg, key, default):
    # type: (Dict[str, Any], str, int) -> int
    try:
        return int(cfg.get(key, default))
    except Exception:
        return default
