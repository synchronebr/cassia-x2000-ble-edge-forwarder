import json
import os
from typing import Any, Dict, Tuple, Optional


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cfg(app_name: str, base_dir: str, env_cfg_path: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """
    Carrega a config em CAMADAS, mesclando de baixo para cima (a de cima
    sobrescreve a de baixo, chave a chave):

      1) <base_dir>/config.json               (base, vem no pacote; SEM segredo)
      2) /root/config/<app_name>/config.json  (overlay por gateway; ex.: api_key)
      3) env_cfg_path (ex.: APP_CONFIG)        (override pontual)

    Camadas ausentes são ignoradas. Pelo menos uma precisa existir.
    Retorna (config_mesclada, caminhos_usados_separados_por_virgula).
    """
    layers = [
        os.path.join(base_dir, "config.json"),
        "/root/config/{}/config.json".format(app_name),
    ]
    if env_cfg_path:
        layers.append(env_cfg_path)

    merged: Dict[str, Any] = {}
    used = []
    seen = set()
    for p in layers:
        if not p or not os.path.exists(p):
            continue
        # evita aplicar o mesmo arquivo duas vezes (ex.: autorun exporta
        # APP_CONFIG=<base_dir>/config.json, que já é a camada base). Reaplicá-lo
        # por último sobrescreveria o segredo vindo de /root/config.
        real = os.path.realpath(p)
        if real in seen:
            continue
        seen.add(real)
        merged.update(load_json(p))
        used.append(p)

    if not used:
        raise FileNotFoundError("Nenhum config.json encontrado. Tentados: {}".format(layers))

    return merged, ",".join(used)


def get_str(cfg: Dict[str, Any], key: str, default: str = "") -> str:
    v = cfg.get(key, default)
    return "" if v is None else str(v)


def get_int(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except Exception:
        return default


def get_float(cfg: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except Exception:
        return default