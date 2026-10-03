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


def _clock(minutes) -> str | None:
    return f"{minutes // 60:02d}:{minutes % 60:02d}" if isinstance(minutes, int) else None


PORT_LIGHTS = {
    "Blue": "luce blu, in attesa",
    "Green": "luce verde, carica completata",
    "FlashingGreen": "luce verde lampeggiante, in carica",
    "Amber": "luce ambra, cavo non inserito bene",
    "FlashingAmber": "luce ambra lampeggiante, carica ridotta",
    "Red": "luce rossa, errore",
    "White": "luce bianca",
    "Off": "luce spenta",
}


def _status_rows(cs: dict, vs: dict, climate: dict) -> list[dict]:
    """Righe di stato per il pannello dell'auto. Compaiono solo i dati che l'auto fornisce."""
    rows = []

    def add(icon, title, value, sub=""):
        if value is not None:
            rows.append({"icon": icon, "title": title, "value": value, "sub": sub})

    if cs.get("charge_port_door_open") is not None:
        latch = "cavo bloccato" if cs.get("charge_port_latch") == "Engaged" else "cavo libero"
        light = PORT_LIGHTS.get(cs.get("charge_port_color"), "")
        add(
            "ev_station", "Sportello di ricarica",
            "Aperto" if cs["charge_port_door_open"] else "Chiuso",
            ", ".join(x for x in (latch, light) if x).capitalize(),
        )
    start = _clock(cs.get("scheduled_charging_start_time_minutes"))
    if cs.get("scheduled_charging_pending") and start:
        add("schedule", "Carica programmata sull'auto", start, "È l'orario che Octopus ha impostato per stanotte")
    departure = _clock(cs.get("scheduled_departure_time_minutes"))
    if cs.get("preconditioning_enabled") and departure:
        days = "nei giorni feriali" if cs.get("preconditioning_times") == "weekdays" else "tutti i giorni"
        add("mode_heat", "Abitacolo pronto alla partenza", departure, f"Climatizzazione anticipata {days}")
    if cs.get("battery_heater_on") is not None:
        add(
            "thermostat", "Riscaldamento della batteria",
            "Attivo" if cs["battery_heater_on"] else "Spento",
            "Quando è attivo, parte dell'energia di ricarica scalda la batteria",
        )
    if climate.get("is_climate_on") is not None:
        setting = climate.get("driver_temp_setting")
        add(
            "ac_unit", "Climatizzatore",
            "Acceso" if climate["is_climate_on"] else "Spento",
            f"Impostato a {setting:.0f}°" if setting is not None else "",
        )
    if climate.get("cabin_overheat_protection"):
        add(
            "heat", "Protezione dal surriscaldamento",
            {"On": "Attiva", "Off": "Spenta", "FanOnly": "Solo ventola"}.get(
                climate["cabin_overheat_protection"], climate["cabin_overheat_protection"]
            ),
            "Raffredda l'abitacolo quando l'auto è parcheggiata al sole",
        )
    if vs.get("sentry_mode") is not None:
        add("visibility", "Modalità Sentinella", "Attiva" if vs["sentry_mode"] else "Spenta")
    if vs.get("dashcam_state"):
        add(
            "videocam", "Dashcam",
            {"Recording": "Registra", "Unavailable": "Non disponibile", "Off": "Spenta"}.get(
                vs["dashcam_state"], vs["dashcam_state"]
            ),
        )
    openings = {
        "finestrini": [vs.get(k) for k in ("fd_window", "fp_window", "rd_window", "rp_window")],
        "porte": [vs.get(k) for k in ("df", "dr", "pf", "pr")],
        "bagagliai": [vs.get(k) for k in ("ft", "rt")],
    }
    if any(v is not None for values in openings.values() for v in values):
        open_parts = [name for name, values in openings.items() if any(values)]
        add(
            "sensor_door", "Porte, finestrini e bagagliai",
            "Aperti" if open_parts else "Chiusi",
            ("Aperti: " + ", ".join(open_parts)) if open_parts else "",
        )
    if vs.get("is_user_present") is not None:
        add("person", "Qualcuno a bordo", "Sì" if vs["is_user_present"] else "No")
    update = vs.get("software_update") or {}
    if update.get("status"):
        version = (update.get("version") or "").strip()
        add(
            "system_update", "Aggiornamento software",
            f"{update.get('download_perc', 0)}%",
            f"Versione {version} in arrivo" if version else "In arrivo",
        )
    if vs.get("valet_mode"):
        add("key", "Modalità parcheggiatore", "Attiva")
    if vs.get("service_mode"):
        add("build", "Modalità assistenza", "Attiva")
    return rows


DRIVER_ASSIST = {"TeslaAP3": "Hardware 3", "TeslaAP4": "Hardware 4"}


def _specs(vs: dict, config: dict) -> list[dict]:
    """Dati tecnici e curiosità, nei codici interni di Tesla."""
    specs = []

    def add(label, value):
        if value not in (None, "", "None"):
            specs.append({"label": label, "value": str(value)})

    add("Motore posteriore", config.get("rear_drive_unit"))
    assist = config.get("driver_assist")
    add("Computer di guida", f"{DRIVER_ASSIST[assist]} ({assist})" if assist in DRIVER_ASSIST else assist)
    add("Pacchetto di efficienza", config.get("efficiency_package"))
    add("Allestimento", config.get("trim_badging"))
    add("Colore", config.get("exterior_color"))
    add("Cerchi", config.get("wheel_type"))
    add("Presa di ricarica", config.get("charge_port_type"))
    add("Sportello motorizzato", {True: "Sì", False: "No"}.get(config.get("motorized_charge_port")))
    add("Versione delle API dell'auto", vs.get("api_version"))
    if vs.get("santa_mode") is not None:
        add("Modalità Babbo Natale", "Attiva" if vs["santa_mode"] else "Spenta")
    return specs


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
                "?endpoints=charge_state%3Bvehicle_state%3Bclimate_state%3Bvehicle_config"
            )
        except HttpError as err:
            if err.status == 408:
                return None
            raise
        cs = data["response"]["charge_state"]
        vs = data["response"].get("vehicle_state") or {}
        climate = data["response"].get("climate_state") or {}
        config = data["response"].get("vehicle_config") or {}
        update = vs.get("software_update") or {}

        def any_open(*keys):
            values = [vs.get(k) for k in keys]
            return None if all(v is None for v in values) else any(values)
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
            "voltage": cs.get("charger_voltage"),
            "battery_heater": cs.get("battery_heater_on"),
            "port_open": cs.get("charge_port_door_open"),
            "port_latch": cs.get("charge_port_latch"),
            "sentry": vs.get("sentry_mode"),
            "windows_open": any_open("fd_window", "fp_window", "rd_window", "rp_window"),
            "doors_open": any_open("df", "dr", "pf", "pr"),
            "trunks_open": any_open("ft", "rt"),
            "user_present": vs.get("is_user_present"),
            "update_status": update.get("status") or None,
            "update_version": (update.get("version") or "").strip() or None,
            "update_percent": update.get("download_perc"),
            "car_type": config.get("car_type"),
            "trim": config.get("trim_badging"),
            "color": config.get("exterior_color"),
            "wheels": config.get("wheel_type"),
        }
        self.last_info.update(
            usable_level=cs.get("usable_battery_level"),
            full_charges_in_a_row=cs.get("max_range_charge_counter"),
            pilot_amps=cs.get("charger_pilot_current"),
            status_rows=_status_rows(cs, vs, climate),
            specs=_specs(vs, config),
        )
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
