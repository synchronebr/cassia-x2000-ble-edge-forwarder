import json
import urllib.request
import urllib.error


def post_json(url, payload, api_key="", api_key_header="X-API-Key", timeout=5):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
    }

    if api_key:
        headers[api_key_header] = api_key

    req = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status < 200 or status >= 300:
                raise RuntimeError("HTTP status inválido: %s" % status)
    except urllib.error.HTTPError as e:
        response_body = ""
        try:
            response_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError("HTTPError %s: %s" % (e.code, response_body))
    except urllib.error.URLError as e:
        raise RuntimeError("URLError: %s" % str(e))
    except Exception as e:
        raise RuntimeError("Erro no POST JSON: %s" % str(e))