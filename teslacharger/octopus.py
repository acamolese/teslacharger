"""Stato del veicolo e carica immediata tramite l'API di Octopus Energy Italia."""

import os
import time
from dataclasses import dataclass

from .http import request_json

ENDPOINT = "https://api.oeit-kraken.energy/v1/graphql/"
# Il token Kraken dura un'ora: lo rinnoviamo con largo anticipo
TOKEN_LIFETIME_SECONDS = 45 * 60
STATE_BOOSTING = "BOOSTING"
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
# Stato osservato quando l'auto non è collegata alla presa di casa
STATE_UNPLUGGED = "SMART_CONTROL_NOT_AVAILABLE"


@dataclass(frozen=True)
class VehicleStatus:
    device_id: str
    name: str
    state: str
    suspended: bool
    # Livello di carica che Octopus deve raggiungere e ora entro cui farlo
    target_percent: int | None = None
    target_time: str | None = None

    @property
    def boosting(self) -> bool:
        return self.state == STATE_BOOSTING

    @property
    def plugged(self) -> bool:
        return self.state != STATE_UNPLUGGED


class OctopusClient:
    def __init__(self, email: str | None = None, password: str | None = None):
        self._email = email or os.environ["OCTOPUS_EMAIL"]
        self._password = password or os.environ["OCTOPUS_PASSWORD"]
        self._token: str | None = None
        self._token_time = 0.0
        self._account: str | None = None

    def _gql(self, query: str, variables: dict | None = None, *, auth: bool = True) -> dict:
        headers = {}
        if auth:
            if not self._token or time.time() - self._token_time > TOKEN_LIFETIME_SECONDS:
                self._login()
            headers["Authorization"] = self._token
        data = request_json(
            ENDPOINT, body={"query": query, "variables": variables or {}}, headers=headers
        )
        if data.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in data["errors"])
            raise RuntimeError(f"Octopus: {messages}")
        return data["data"]

    def _login(self) -> None:
        data = self._gql(
            """mutation ($email: String!, $password: String!) {
              obtainKrakenToken(input: { email: $email, password: $password }) { token }
            }""",
            {"email": self._email, "password": self._password},
            auth=False,
        )
        self._token = data["obtainKrakenToken"]["token"]
        self._token_time = time.time()

    def _account_number(self) -> str:
        if self._account is None:
            accounts = self._gql("query { viewer { accounts { number } } }")["viewer"]["accounts"]
            if not accounts:
                raise RuntimeError("Octopus: nessun account trovato")
            self._account = accounts[0]["number"]
        return self._account

    def vehicle(self) -> VehicleStatus:
        devices = self._gql(
            """query ($accountNumber: String!) {
              devices(accountNumber: $accountNumber) {
                id name deviceType
                status { currentState isSuspended }
                preferences { schedules { dayOfWeek max time } }
              }
            }""",
            {"accountNumber": self._account_number()},
        )["devices"]
        for device in devices or []:
            if device["deviceType"] == "ELECTRIC_VEHICLES":
                schedules = (device.get("preferences") or {}).get("schedules") or []
                return VehicleStatus(
                    device_id=device["id"],
                    name=device["name"],
                    state=device["status"]["currentState"],
                    suspended=bool(device["status"]["isSuspended"]),
                    target_percent=int(schedules[0]["max"]) if schedules else None,
                    target_time=schedules[0]["time"][:5] if schedules else None,
                )
        raise RuntimeError("Octopus: nessun veicolo registrato in Intelligent Octopus")

    def set_target(self, device_id: str, percent: int, ready_time: str) -> None:
        """Imposta per tutti i giorni il livello di carica da raggiungere entro l'ora indicata."""
        if not 10 <= percent <= 100:
            raise ValueError("il livello di carica deve essere tra 10 e 100")
        self._gql(
            """mutation ($input: SmartFlexDevicePreferencesInput!) {
              setDevicePreferences(input: $input) { id }
            }""",
            {
                "input": {
                    "deviceId": device_id,
                    "mode": "CHARGE",
                    "unit": "PERCENTAGE",
                    "schedules": [
                        {"dayOfWeek": day, "time": ready_time, "max": percent}
                        for day in WEEKDAYS
                    ],
                }
            },
        )

    def _boost(self, device_id: str, action: str) -> None:
        self._gql(
            """mutation ($input: UpdateBoostChargeInput!) {
              updateBoostCharge(input: $input) { id }
            }""",
            {"input": {"deviceId": device_id, "action": action}},
        )

    def start_boost(self, device_id: str) -> None:
        self._boost(device_id, "BOOST")

    def cancel_boost(self, device_id: str) -> None:
        self._boost(device_id, "CANCEL")
