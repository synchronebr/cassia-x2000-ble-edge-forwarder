import json
import urllib.request
import urllib.error


def fetch_cassia_info(base_url: str, timeout: int = 5) -> dict:
    url = base_url.rstrip("/") + "/cassia/info"

    req = urllib.request.Request(
        url=url,
        headers={"Accept": "application/json"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status < 200 or status >= 300:
                raise RuntimeError("cassia/info status inválido: %s" % status)

            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)

            if not isinstance(data, dict):
                raise RuntimeError("cassia/info retornou payload não-objeto")

            return data

    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError("cassia/info HTTPError %s: %s" % (e.code, body))
    except urllib.error.URLError as e:
        raise RuntimeError("cassia/info URLError: %s" % str(e))
    except json.JSONDecodeError as e:
        raise RuntimeError("cassia/info JSON inválido: %s" % str(e))
    except Exception as e:
        raise RuntimeError("cassia/info erro: %s" % str(e))


def normalize_gateway_identity(info: dict) -> dict:
    """
    Mantém comportamento defensivo porque o nome dos campos pode variar
    conforme firmware/modelo.
    """
    gateway_mac = (
        info.get("ap_mac")
        or info.get("apMac")
        or info.get("mac")
        or info.get("router_mac")
        or info.get("gatewayMac")
        or info.get("bt_mac")
        or ""
    )

    return {
        "gatewayMac": gateway_mac,
        "apMac": gateway_mac,
        "gatewayIp": info.get("ip") or info.get("gatewayIp") or "",
        "model": info.get("model") or "",
        "name": info.get("name") or "",
        "version": info.get("version") or info.get("firmware") or "",
        "raw": info,
    }