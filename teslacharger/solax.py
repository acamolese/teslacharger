"""Lettura dell'impianto fotovoltaico dalla Solax Developer API."""

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .http import request_json

BASE = "https://openapi-eu.solaxcloud.com"
BUSINESS_RESIDENTIAL = 1
DEVICE_INVERTER = 1
DEVICE_BATTERY = 2
SUCCESS = 10000
INVALID_TOKEN = 10402
STAT_BY_MONTH = 2
MAX_HOLD_SECONDS = 60000
# Valori ammessi dall'API per "cosa fare allo scadere": 160 e 161. Nella documentazione
# Modbus di Solax 0xA0 (160) è l'uscita dalla modalità remota. Significato da confermare
# sulla documentazione ufficiale del portale sviluppatori.
NEXT_EXIT_REMOTE = 160
ALARM_NAMES = {
    "Grid Volt Fault": "Tensione di rete fuori dai limiti",
    "Grid Freq Fault": "Frequenza di rete fuori dai limiti",
    "Grid Lost Fault": "Rete assente",
    "Bus Volt Fault": "Tensione interna fuori dai limiti",
    "Bat Volt Fault": "Tensione della batteria fuori dai limiti",
    "Over Load Fault": "Sovraccarico",
    "Temp Over Fault": "Temperatura troppo alta",
}


@dataclass(frozen=True)
class PlantSnapshot:
    # Ora locale dell'impianto a cui si riferisce il dato (si aggiorna ogni 5 minuti)
    data_time: str
    pv_w: float
    # Potenza in uscita dall'inverter verso casa e rete
    inverter_ac_w: float
    # Scambio con la rete: positivo = immissione, negativo = prelievo
    # (verificato sui contatori di energia importata ed esportata)
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

    def _request(self, path: str, **kwargs) -> dict:
        """Chiamata autenticata. Se il token non vale più, ne chiede uno nuovo e riprova.

        Solax tiene valido un solo token per applicazione: basta che un altro programma
        con le stesse credenziali ne chieda uno perché quello in memoria venga annullato.
        """
        data = request_json(BASE + path, headers=self._auth(), **kwargs)
        if data.get("code") == INVALID_TOKEN:
            self._token = None
            data = request_json(BASE + path, headers=self._auth(), **kwargs)
        return data

    def _get(self, path: str, params: dict):
        data = self._request(path, params=params)
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

    # --- comandi all'inverter ---

    def _command(self, path: str, body: dict) -> dict:
        data = self._request(
            path,
            body={"snList": [self._device_sns()[DEVICE_INVERTER]], "businessType": BUSINESS_RESIDENTIAL, **body},
        )
        if data.get("code") != SUCCESS:
            raise RuntimeError(f"Solax: comando rifiutato ({data.get('code')} {data.get('message')})")
        return data

    def hold_battery(self, seconds: int) -> dict:
        """Impedisce alla batteria di casa di scaricarsi per il tempo indicato.

        Usa la modalità remota "solo carica": la batteria può caricarsi dai pannelli ma
        non cede energia. Allo scadere l'inverter esce dalla modalità remota e torna al
        funzionamento di prima. Durata ammessa dall'API: da 1 a 60000 secondi.
        """
        if not 1 <= seconds <= MAX_HOLD_SECONDS:
            raise ValueError("durata non ammessa")
        return self._command(
            "/openapi/v2/device/inverter_vpp_mode/self_consume/charge_only_mode",
            {"timeOfDuration": seconds, "nextMotion": NEXT_EXIT_REMOTE},
        )

    def release_battery(self) -> dict:
        """Esce subito dalla modalità remota e ripristina il funzionamento normale."""
        return self._command("/openapi/v2/device/inverter_vpp_mode/exit_vpp_mode", {})

    # --- dati per il pannello della casa ---

    def _plant_id(self) -> str:
        if getattr(self, "_plant", None) is None:
            self._device_sns()
            self._plant = self._get(
                "/openapi/v2/plant/page_plant_info",
                {"businessType": BUSINESS_RESIDENTIAL, "pageNo": 1},
            )["records"][0]["plantId"]
        return self._plant

    def _month_stats(self, month: str) -> list[dict]:
        data = self._request(
            "/openapi/v2/plant/energy/get_stat_data",
            body={
                "plantId": self._plant_id(),
                "dateType": STAT_BY_MONTH,
                "date": month,
                "businessType": BUSINESS_RESIDENTIAL,
            },
        )
        if data.get("code") != SUCCESS:
            raise RuntimeError(f"Solax: statistiche non disponibili ({data.get('message')})")
        return data["result"].get("plantEnergyStatDataList") or []

    def month_totals(self, month: str) -> dict:
        """Energia del mese (formato AAAA-MM): prelevata dalla rete, prodotta e ceduta, in kWh."""
        days = self._month_stats(month)
        return {
            "imported": round(sum(d.get("importEnergy") or 0 for d in days), 1),
            "pv": round(sum(d.get("pvGeneration") or 0 for d in days), 1),
            "exported": round(sum(d.get("exportEnergy") or 0 for d in days), 1),
        }

    def _alarms(self) -> list[dict]:
        records = self._get(
            "/openapi/v2/alarm/page_alarm_info",
            {"plantId": self._plant_id(), "businessType": BUSINESS_RESIDENTIAL, "alarmState": 0, "pageNo": 1},
        )["records"]
        return [
            {
                "name": ALARM_NAMES.get(a.get("alarmName"), a.get("alarmName")),
                "start": a.get("alarmStartTime"),
                "end": a.get("alarmEndTime"),
            }
            for a in records[:6]
        ]

    def home_summary(self, days: int = 14) -> dict:
        """Tutto ciò che l'impianto racconta di sé: giornate, stringhe, batteria, totali, avvisi."""
        today = date.today()
        first = today - timedelta(days=days - 1)
        stats = self._month_stats(today.strftime("%Y-%m"))
        if first.month != today.month:
            stats = self._month_stats(first.strftime("%Y-%m")) + stats
        by_day = {s["date"]: s for s in stats}
        series = []
        for i in range(days):
            day = (first + timedelta(days=i)).isoformat()
            s = by_day.get(day, {})
            series.append({
                "day": day,
                "pv": s.get("pvGeneration") or 0,
                "load": s.get("loadConsumption") or 0,
                "imported": s.get("importEnergy") or 0,
                "exported": s.get("exportEnergy") or 0,
                "battery_in": s.get("batteryCharged") or 0,
                "battery_out": s.get("batteryDischarged") or 0,
            })
        inverter = self._realtime(DEVICE_INVERTER)
        battery = self._realtime(DEVICE_BATTERY)
        mppt = inverter.get("mpptMap") or {}
        strings = [
            {"w": mppt.get(f"MPPT{n}Power"), "v": mppt.get(f"MPPT{n}Voltage"), "a": mppt.get(f"MPPT{n}Current")}
            for n in (1, 2, 3, 4)
            if mppt.get(f"MPPT{n}Voltage") is not None
        ]
        return {
            "time": inverter.get("plantLocalTime"),
            "fetched": datetime.now().isoformat(timespec="seconds"),
            "days": series,
            "live": {
                "strings": strings,
                "inverter_temp": inverter.get("inverterTemperature"),
                "grid_v": inverter.get("acVoltage1"),
                "grid_hz": inverter.get("acFrequency1"),
                "inverter_w": inverter.get("acPower1"),
                "grid_w": inverter.get("gridPower"),
            },
            "battery": {
                "soc": battery.get("batterySOC"),
                "remaining_kwh": battery.get("batteryRemainings"),
                "soh": battery.get("batterySOH"),
                "cycles": battery.get("batteryCycleTimes"),
                "temp": battery.get("batteryTemperature"),
                "voltage": battery.get("batteryVoltage"),
                "power_w": battery.get("chargeDischargePower"),
                "charged_kwh": battery.get("totalDeviceCharge"),
                "discharged_kwh": battery.get("totalDeviceDischarge"),
            },
            "lifetime": {
                "pv_kwh": inverter.get("totalYield"),
                "imported_kwh": inverter.get("totalImportEnergy"),
                "exported_kwh": inverter.get("totalExportEnergy"),
            },
            "alarms": self._alarms(),
        }


def main() -> None:
    """Comandi manuali: stato, blocco della scarica, sblocco.

    python3 -m teslacharger.solax status
    python3 -m teslacharger.solax hold SECONDI
    python3 -m teslacharger.solax release
    """
    import sys

    from .config import load_env

    load_env()
    client = SolaxClient()
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "hold" and len(sys.argv) > 2:
        print(client.hold_battery(int(sys.argv[2])))
    elif command == "release":
        print(client.release_battery())
    snap = client.snapshot()
    print(
        f"dato delle {snap.data_time[11:16]}: batteria {snap.battery_soc}% {snap.battery_w:+.0f} W, "
        f"rete {snap.grid_w:+.0f} W, pannelli {snap.pv_w:.0f} W"
    )


if __name__ == "__main__":
    main()
