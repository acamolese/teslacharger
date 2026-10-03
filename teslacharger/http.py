"""Chiamate HTTP con la sola libreria standard."""

import json
import urllib.parse
import urllib.request

TIMEOUT = 30


def request_json(url: str, *, params=None, form=None, body=None, headers=None) -> dict:
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
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return json.load(res)
