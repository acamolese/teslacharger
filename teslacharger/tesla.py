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
from .http import HttpError, request_json
from .policy import CarStatus

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


def _km(miles) -> float | None:
    return round(miles * 1.609344, 1) if miles is not None else None


class TeslaCar:
    """Lettura dello stato di carica e comandi all'auto.

    Le letture vanno direttamente alla Fleet API. I comandi passano dal programma
    tesla-http-proxy, che li firma con la chiave privata dell'applicazione.
    """

    def __init__(self):
        self._vin: str | None = os.environ.get("TESLA_VIN") or None
        self._proxy = os.environ.get("TESLA_PROXY_URL", "https://localhost:4443")
        self._proxy_cert = os.environ.get("TESLA_PROXY_CERT")
        # Dati dell'ultima lettura, per il pannello dell'auto
        self.last_info: dict | None = None

    def vin(self) -> str:
        if self._vin is None:
            self._vin = api_get("/api/1/vehicles")["response"][0]["vin"]
        return self._vin

    def status(self) -> CarStatus | None:
        """Stato della ricarica, oppure None se l'auto è in standby o non raggiungibile."""
        try:
            data = api_get(
                f"/api/1/vehicles/{self.vin()}/vehicle_data"
                "?endpoints=charge_state%3Bvehicle_state%3Bclimate_state"
            )
        except HttpError as err:
            if err.status == 408:
                return None
            raise
        cs = data["response"]["charge_state"]
        vs = data["response"].get("vehicle_state") or {}
        climate = data["response"].get("climate_state") or {}
        self.last_info = {
            "name": vs.get("vehicle_name"),
            "level": cs.get("battery_level"),
            "limit": cs.get("charge_limit_soc"),
            "range_km": _km(cs.get("battery_range")),
            "odometer_km": _km(vs.get("odometer")),
            "charging": cs.get("charging_state") == "Charging",
            "charging_state": cs.get("charging_state"),
            "amps": cs.get("charger_actual_current"),
            "energy_added_kwh": cs.get("charge_energy_added"),
            "minutes_to_full": cs.get("minutes_to_full_charge"),
            "inside_temp": climate.get("inside_temp"),
            "outside_temp": climate.get("outside_temp"),
            "software": (vs.get("car_version") or "").split(" ")[0] or None,
            "locked": vs.get("locked"),
        }
        return CarStatus(
            plugged=cs.get("charging_state") not in (None, "Disconnected"),
            charging=cs.get("charging_state") == "Charging",
            level=int(cs.get("battery_level") or 0),
            limit=int(cs.get("charge_limit_soc") or 100),
            amps=int(cs.get("charge_current_request") or 0),
            max_amps=int(cs.get("charge_current_request_max") or 0),
            voltage=int(cs.get("charger_voltage") or 0),
        )

    def wake(self) -> None:
        request_json(
            f"{API}/api/1/vehicles/{self.vin()}/wake_up",
            headers={"Authorization": f"Bearer {access_token()}"},
            method="POST",
        )

    def set_amps(self, amps: int) -> None:
        data = request_json(
            f"{self._proxy}/api/1/vehicles/{self.vin()}/command/set_charging_amps",
            body={"charging_amps": amps},
            headers={"Authorization": f"Bearer {access_token()}"},
            cafile=self._proxy_cert,
        )
        if not data.get("response", {}).get("result"):
            raise RuntimeError(f"Tesla: comando rifiutato ({data})")


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
