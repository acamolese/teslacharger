"""Lettura dell'impianto fotovoltaico dalla Solax Developer API."""

import os
import time
from dataclasses import dataclass

from .http import request_json

BASE = "https://openapi-eu.solaxcloud.com"
BUSINESS_RESIDENTIAL = 1
DEVICE_INVERTER = 1
DEVICE_BATTERY = 2
SUCCESS = 10000


@dataclass(frozen=True)
class PlantSnapshot:
    # Ora locale dell'impianto a cui si riferisce il dato (si aggiorna ogni 5 minuti)
    data_time: str
    pv_w: float
    # Potenza in uscita dall'inverter verso casa e rete
    inverter_ac_w: float
    # Scambio con la rete. Segno da confermare sul campo: si assume positivo = immissione
    grid_w: float
    # Positivo = la batteria di casa si sta caricando, negativo = si sta scaricando
    battery_w: float
    battery_soc: int

    @property
    def excess_w(self) -> float:
        """Potenza che avanza dopo i consumi di casa (negativa se casa è in deficit)."""
        return self.battery_w + self.grid_w


class SolaxClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self._client_id = client_id or os.environ["SOLAX_CLIENT_ID"]
        self._client_secret = client_secret or os.environ["SOLAX_CLIENT_SECRET"]
        self._token: str | None = None
        self._token_expiry = 0.0
        self._devices: dict[int, str] | None = None

    def _auth(self) -> dict:
        if not self._token or time.time() > self._token_expiry - 3600:
            data = request_json(
                f"{BASE}/openapi/auth/oauth/token",
                form={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
            if data.get("code") != 0:
                raise RuntimeError(f"Solax: token non ottenuto ({data.get('message')})")
            self._token = data["result"]["access_token"]
            self._token_expiry = time.time() + int(data["result"].get("expires_in") or 3600)
        return {"Authorization": f"bearer {self._token}"}

    def _get(self, path: str, params: dict):
        data = request_json(BASE + path, params=params, headers=self._auth())
        if data.get("code") != SUCCESS:
            raise RuntimeError(f"Solax: errore su {path} ({data.get('code')} {data.get('message')})")
        return data["result"]

    def _device_sns(self) -> dict[int, str]:
        """Numeri di serie di inverter e batteria del primo impianto dell'account."""
        if self._devices is None:
            plants = self._get(
                "/openapi/v2/plant/page_plant_info",
                {"businessType": BUSINESS_RESIDENTIAL, "pageNo": 1},
            )["records"]
            if not plants:
                raise RuntimeError("Solax: nessun impianto visibile all'applicazione")
            devices = {}
            for device_type in (DEVICE_INVERTER, DEVICE_BATTERY):
                records = self._get(
                    "/openapi/v2/device/page_device_info",
                    {
                        "businessType": BUSINESS_RESIDENTIAL,
                        "deviceType": device_type,
                        "pageNo": 1,
                        "plantId": plants[0]["plantId"],
                    },
                )["records"]
                if not records:
                    raise RuntimeError(f"Solax: nessun dispositivo di tipo {device_type}")
                devices[device_type] = records[0]["deviceSn"]
            self._devices = devices
        return self._devices

    def _realtime(self, device_type: int) -> dict:
        result = self._get(
            "/openapi/v2/device/realtime_data",
            {
                "snList": self._device_sns()[device_type],
                "deviceType": device_type,
                "businessType": BUSINESS_RESIDENTIAL,
            },
        )
        if not result:
            raise RuntimeError(f"Solax: nessun dato in tempo reale per il tipo {device_type}")
        return result[0]

    def snapshot(self) -> PlantSnapshot:
        inverter = self._realtime(DEVICE_INVERTER)
        battery = self._realtime(DEVICE_BATTERY)
        return PlantSnapshot(
            data_time=inverter["plantLocalTime"],
            pv_w=float(inverter.get("MPPTTotalInputPower") or 0),
            inverter_ac_w=float(inverter.get("acPower1") or 0),
            grid_w=float(inverter.get("gridPower") or 0),
            battery_w=float(battery.get("chargeDischargePower") or 0),
            battery_soc=int(battery["batterySOC"]),
        )
