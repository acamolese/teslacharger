"""Stato del veicolo e carica immediata tramite l'API di Octopus Energy Italia."""

import os
import time
from dataclasses import dataclass

from .http import request_json

ENDPOINT = "https://api.oeit-kraken.energy/v1/graphql/"
# Il token Kraken dura un'ora: lo rinnoviamo con largo anticipo
TOKEN_LIFETIME_SECONDS = 45 * 60
STATE_BOOSTING = "BOOSTING"
# Orari di fine carica accettati da Octopus: dalle 04:00 alle 11:00, ogni mezz'ora
READY_TIMES = tuple(f"{h:02d}:{m:02d}" for h in range(4, 12) for m in (0, 30) if (h, m) <= (11, 0))
# Livello di carica più basso accettato: sotto il livello dell'auto, di notte non parte nulla
MIN_TARGET = 10
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
# Stato osservato quando l'auto non è collegata alla presa di casa
STATE_UNPLUGGED = "SMART_CONTROL_NOT_AVAILABLE"
# Stato con un piano di carica in corso: Octopus ha preso in carico l'auto
STATE_PLANNED = "SMART_CONTROL_IN_PROGRESS"


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

    def billing(self) -> dict:
        """Saldo e ultimi movimenti del conto dell'energia elettrica, in euro."""
        ledgers = self._gql(
            """query ($accountNumber: String!) {
              account(accountNumber: $accountNumber) {
                ledgers {
                  ledgerType balance amountOwedByCustomer
                  transactions(first: 30) {
                    edges { node { __typename postedDate amounts { gross } } }
                  }
                }
              }
            }""",
            {"accountNumber": self._account_number()},
        )["account"]["ledgers"]
        ledger = next((l for l in ledgers if "ELECTRICITY" in (l.get("ledgerType") or "")), None)
        if ledger is None:
            raise RuntimeError("Octopus: conto dell'energia elettrica non trovato")
        charges, payments = [], []
        for edge in ledger["transactions"]["edges"]:
            node = edge["node"]
            entry = {"date": node["postedDate"], "amount": node["amounts"]["gross"] / 100}
            (charges if node["__typename"] == "Charge" else payments).append(entry)
        return {
            "owed": (ledger.get("amountOwedByCustomer") or 0) / 100,
            "charges": charges,
            "payments": payments,
        }

    def planned_dispatches(self, device_id: str) -> list[dict]:
        """Finestre di carica pianificate da Octopus per le prossime ore."""
        rows = self._gql(
            """query ($deviceId: String!) {
              flexPlannedDispatches(deviceId: $deviceId) { start end type }
            }""",
            {"deviceId": device_id},
        )["flexPlannedDispatches"]
        return rows or []

    def completed_dispatches(self) -> list[dict]:
        """Ore di carica già consuntivate da Octopus, con l'energia erogata."""
        rows = self._gql(
            """query ($accountNumber: String!) {
              completedDispatches(accountNumber: $accountNumber) { start end delta }
            }""",
            {"accountNumber": self._account_number()},
        )["completedDispatches"]
        return [
            {"start": r["start"], "end": r["end"], "kwh": abs(float(r["delta"] or 0))}
            for r in rows or []
        ]

    def charging_sessions(self, last: int = 20) -> list[dict]:
        """Ultime sessioni di ricarica chiuse, come compaiono nell'app di Octopus."""
        devices = self._gql(
            """query ($accountNumber: String!, $last: Int!) {
              devices(accountNumber: $accountNumber) {
                ... on SmartFlexVehicle {
                  chargingSessions(last: $last) { edges { node {
                    start end stateOfChargeChange stateOfChargeFinal energyAdded { value }
                    ... on SmartFlexChargingSession {
                      type
                      problems {
                        ... on SmartFlexChargingError { cause }
                        ... on SmartFlexChargingTruncation { truncationCause }
                      }
                    }
                  } } }
                }
              }
            }""",
            {"accountNumber": self._account_number(), "last": last},
        )["devices"]
        sessions = []
        for device in devices or []:
            for edge in (device.get("chargingSessions") or {}).get("edges") or []:
                node = edge["node"]
                problems = [p.get("cause") or p.get("truncationCause") for p in node.get("problems") or []]
                sessions.append({
                    "start": node["start"],
                    "end": node["end"],
                    "type": node.get("type"),
                    "kwh": float((node.get("energyAdded") or {}).get("value") or 0),
                    "soc_change": float(node["stateOfChargeChange"]) if node.get("stateOfChargeChange") else None,
                    "soc_final": float(node["stateOfChargeFinal"]) if node.get("stateOfChargeFinal") else None,
                    "problems": ",".join(p for p in problems if p),
                })
        return sessions

    def set_target(self, device_id: str, percent: int, ready_time: str) -> None:
        """Imposta per tutti i giorni il livello di carica da raggiungere entro l'ora indicata."""
        if not MIN_TARGET <= percent <= 100:
            raise ValueError(f"il livello di carica deve essere tra {MIN_TARGET} e 100")
        if ready_time not in READY_TIMES:
            raise ValueError("orario di fine carica non ammesso")
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
