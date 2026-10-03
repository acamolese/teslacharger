"""Accesso alla Tesla Fleet API: registrazione dell'applicazione e autorizzazione.

Uso:
  python3 -m teslacharger.tesla register          registra il dominio presso Tesla (una tantum)
  python3 -m teslacharger.tesla auth-url          stampa il link con cui autorizzare l'account
  python3 -m teslacharger.tesla exchange CODICE   scambia il codice ricevuto con i token
  python3 -m teslacharger.tesla vehicles          elenca i veicoli dell'account
"""

import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

from .config import load_env
from .http import request_json

AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
# Regione Europa, Medio Oriente e Africa
API = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
SCOPES = "openid offline_access vehicle_device_data vehicle_charging_cmds"


def _domain() -> str:
    return os.environ.get("TESLA_DOMAIN", "tesla.kilowattzero.it")


def _redirect_uri() -> str:
    return f"https://{_domain()}/auth/callback"


def _token_file() -> Path:
    return Path(os.environ.get("TESLA_TOKEN_FILE", "tesla-tokens.json"))


def _client() -> dict:
    return {
        "client_id": os.environ["TESLA_CLIENT_ID"],
        "client_secret": os.environ["TESLA_CLIENT_SECRET"],
    }


def partner_token() -> str:
    data = request_json(
        TOKEN_URL,
        form={**_client(), "grant_type": "client_credentials", "scope": SCOPES, "audience": API},
    )
    return data["access_token"]


def register() -> dict:
    """Registra il dominio dell'applicazione: Tesla scarica da lì la chiave pubblica."""
    headers = {"Authorization": f"Bearer {partner_token()}"}
    request_json(f"{API}/api/1/partner_accounts", body={"domain": _domain()}, headers=headers)
    return request_json(
        f"{API}/api/1/partner_accounts/public_key", params={"domain": _domain()}, headers=headers
    )


def auth_url() -> str:
    params = {
        "response_type": "code",
        "client_id": os.environ["TESLA_CLIENT_ID"],
        "redirect_uri": _redirect_uri(),
        "scope": SCOPES,
        "state": secrets.token_urlsafe(16),
        "prompt_missing_scopes": "true",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _save(tokens: dict) -> None:
    tokens["obtained_at"] = int(time.time())
    path = _token_file()
    path.write_text(json.dumps(tokens))
    path.chmod(0o600)


def exchange(code: str) -> None:
    _save(
        request_json(
            TOKEN_URL,
            form={
                **_client(),
                "grant_type": "authorization_code",
                "code": code,
                "audience": API,
                "redirect_uri": _redirect_uri(),
            },
        )
    )


def access_token() -> str:
    """Token dell'utente, rinnovato quando sta per scadere."""
    tokens = json.loads(_token_file().read_text())
    if time.time() > tokens["obtained_at"] + tokens["expires_in"] - 300:
        # Il refresh token è monouso: va salvato subito quello nuovo
        tokens = request_json(
            TOKEN_URL,
            form={
                "grant_type": "refresh_token",
                "client_id": os.environ["TESLA_CLIENT_ID"],
                "refresh_token": tokens["refresh_token"],
            },
        )
        _save(tokens)
    return tokens["access_token"]


def api_get(path: str) -> dict:
    return request_json(API + path, headers={"Authorization": f"Bearer {access_token()}"})


def main() -> None:
    load_env()
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "register":
        key = register()["response"]["public_key"]
        print(f"Dominio {_domain()} registrato, chiave pubblica vista da Tesla: {key[:16]}...")
    elif command == "auth-url":
        print(auth_url())
    elif command == "exchange" and len(sys.argv) > 2:
        exchange(sys.argv[2])
        print(f"Token salvati in {_token_file()}")
    elif command == "vehicles":
        for vehicle in api_get("/api/1/vehicles")["response"]:
            print(vehicle["display_name"], vehicle["vin"][-6:], vehicle["state"])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
