"""Chiamate HTTP con la sola libreria standard."""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 60


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def request_json(
    url: str, *, params=None, form=None, body=None, headers=None, cafile=None, method=None
) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = ssl.create_default_context(cafile=cafile) if cafile else None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=context) as res:
            return json.load(res)
    except urllib.error.HTTPError as err:
        raise HttpError(err.code, err.read().decode(errors="replace")) from err
