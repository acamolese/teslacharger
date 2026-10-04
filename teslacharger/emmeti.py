"""Lettura della pompa di calore Emmeti (sistema Febos) dal portale AQ-IoT.

Il portale non ha un'API documentata: si usano le stesse chiamate della sua app web,
con le credenziali dell'utente. Questo modulo fa solo letture.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE = "https://emmeti.aq-iot.net/aq-iot-server-frontend-ha/api"
TOKEN_LIFETIME_SECONDS = 20 * 60
TIMEOUT = 90

# Contatori di energia, in Wh, che il sistema tiene dall'installazione
COUNTERS = {
    "R8765": "imported",
    "R8766": "exported",
    "R8767": "home",
    "R8768": "pv",
    "R8769": "heat_pump",
    "R8770": "hot_water",
}


class EmmetiClient:
    def __init__(self, username: str | None = None, password: str | None = None):
        self._username = username or os.environ.get("EMMETI_USERNAME", "")
        self._password = password or os.environ.get("EMMETI_PASSWORD", "")
        self._token: str | None = None
        self._token_time = 0.0
        self._ids: tuple[int, int] | None = None

    @property
    def configured(self) -> bool:
        return bool(self._username and self._password)

    def _open(self, url: str, body: dict | None = None, auth: bool = True):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._login()}"
        request = urllib.request.Request(
            url, data=json.dumps(body).encode() if body is not None else None, headers=headers
        )
        try:
            return urllib.request.urlopen(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:160]
            raise RuntimeError(f"Emmeti: HTTP {err.code} {detail}") from err

    def _login(self) -> str:
        if self._token and time.time() - self._token_time < TOKEN_LIFETIME_SECONDS:
            return self._token
        with self._open(
            f"{BASE}/v1/auth/login", {"username": self._username, "password": self._password}, auth=False
        ) as response:
            token = response.headers.get("authorization", "")
        if not token:
            raise RuntimeError("Emmeti: accesso non riuscito")
        self._token = token.removeprefix("Bearer ").strip()
        self._token_time = time.time()
        return self._token

    def _get(self, path: str, params: dict | None = None):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        with self._open(url) as response:
            raw = response.read().decode(errors="replace")
        data = json.loads(raw) if raw[:1] in "[{" else raw
        if isinstance(data, dict) and data.get("errCode"):
            raise RuntimeError(f"Emmeti: {data.get('errCode')} {data.get('message')}")
        return data

    def ids(self) -> tuple[int, int]:
        """Identificativi dell'impianto e della pompa di calore dell'utente."""
        if self._ids is None:
            installation = self._get("/v1/installation")[0]["id"]
            device = self._get("/v1/device", {"filterDict[installationId]": installation})[0]["id"]
            self._ids = (installation, device)
        return self._ids

    def _device(self, path: str, params: dict | None = None):
        """Richiesta inoltrata dal portale al dispositivo di casa."""
        installation, device = self.ids()
        return self._get(f"/v2/emmeti/{installation}/{device}/febos-data/{path}", params)

    def rooms(self) -> list[dict]:
        """Termostati delle stanze: temperatura, temperatura impostata e umidità."""
        rooms = []
        for row in self._device("get-febos-slave"):
            rooms.append({
                "name": str(row.get("nomeSlave", "")).strip(),
                "address": row.get("indirizzoSlave"),
                "temp": int(row["temp"]) / 10 if row.get("temp") is not None else None,
                "set_temp": int(row["setTemp"]) / 10 if row.get("setTemp") is not None else None,
                "humidity": int(row["humid"]) / 10 if row.get("humid") is not None else None,
                "calling": bool(row.get("callTemp")),
                "dehumidifying": bool(row.get("callHumid")),
                "comfort": bool(row.get("confort")),
                "season": row.get("stagione"),
            })
        return rooms

    def energy(self, start: date, end: date) -> dict | None:
        """Energia in kWh tra due date (la seconda esclusa), dai contatori del sistema."""
        rows = self._device("get-data-analysis", {"from": start.isoformat(), "to": end.isoformat()})
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        rows.sort(key=lambda r: r["ts"])
        first, last = rows[0], rows[-1]
        result = {
            name: round((float(last[code]) - float(first[code])) / 1000, 1)
            for code, name in COUNTERS.items()
            if code in first and code in last
        }
        result["other"] = round(result["home"] - result["heat_pump"] - result["hot_water"], 1)
        result["from"], result["to"] = first["ts"], last["ts"]
        return result

    def month_energy(self, year: int, month: int) -> dict | None:
        start = date(year, month, 1)
        return self.energy(start, (start + timedelta(days=32)).replace(day=1))

    # --- stato in tempo reale ---

    def _group_codes(self) -> list[str]:
        """Codici dei gruppi di misure, ricavati dalla configurazione delle pagine del portale."""
        if getattr(self, "_groups", None) is None:
            installation, _ = self.ids()
            config = json.dumps(self._get(f"/v1/installation/{installation}/page-config", {"web": "false"}))
            codes, marker = set(), '"inputGroupGetCode": "'
            for chunk in config.split(marker)[1:]:
                codes.add(chunk.split('"', 1)[0])
            self._groups = sorted(c for c in codes if "GRAPH" not in c)
        return self._groups

    def registers(self) -> dict[str, float]:
        """Valore attuale di tutti i registri del sistema, per codice (R8986, ...)."""
        installation, _ = self.ids()
        groups = self._get(
            f"/v2/emmeti/{installation}/realtime-data", {"input_group_list": ",".join(self._group_codes())}
        )
        values = {}
        for group in groups if isinstance(groups, list) else []:
            for code, cell in (group.get("data") or {}).items():
                if isinstance(cell, dict) and cell:
                    values[code] = next(iter(cell.values()))
        return values

    def power(self) -> dict | None:
        """Ultima potenza registrata, in watt, per pompa di calore, acqua calda, casa e pannelli."""
        today = date.today()
        span = {"from": today.isoformat(), "to": (today + timedelta(days=1)).isoformat(), "rangeSelect": "day"}
        result = {}
        for chart, fields in ((1, {"R8758": "home_w", "R8759": "pv_w"}), (2, {"R8760": "heat_pump_w", "R8761": "hot_water_w"})):
            rows = self._device("get-chart-data", {**span, "chartId": chart})
            if not isinstance(rows, list) or not rows:
                return None
            last = max(rows, key=lambda r: r["ts"])
            result["time"] = last["ts"]
            for code, name in fields.items():
                result[name] = float(last.get(code) or 0)
        return result


def clock(minutes) -> str | None:
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}" if minutes is not None else None


ALARMS = {
    "R9089": "Temperatura di ritorno dell'impianto radiante",
    "R9090": "Accumulo inerziale",
    "R9095": "Temperatura esterna",
    "R9096": "Temperatura di ingresso dell'acqua",
    "R9097": "Temperatura di uscita dell'acqua",
    "R9098": "Temperatura dell'acqua sanitaria",
    "R9099": "Pompa di calore",
    "R9102": "Bassa temperatura dell'acqua",
    "R9103": "Alta temperatura dell'acqua",
    "R9104": "Basso flusso",
}


def describe(reg: dict[str, float]) -> dict:
    """Mette in ordine i registri grezzi: stato, temperature, acqua calda, impostazioni, allarmi."""

    def tenth(code):
        return reg[code] / 10 if code in reg else None

    summer = reg.get("R16385", reg.get("R8683", 0)) == 0
    return {
        "on": bool(reg.get("R16384")),
        "season": "estate" if summer else "inverno",
        "outside_temp": tenth("R8986"),
        "water_out": tenth("R8987"),
        "water_in": tenth("R8988"),
        "water_set": tenth("R9052"),
        "flow_lh": reg.get("R9120"),
        "defrost": bool(reg.get("R8967")),
        "resistance": bool(reg.get("R9074")),
        "antifreeze": bool(reg.get("R9078") or reg.get("R9079")),
        "pv_boost": bool(reg.get("R9076")),
        "voltage": tenth("R8100"),
        "hot_water": {
            "temp": tenth("R8989"),
            "keep": tenth("R16497"),
            "requests": [
                {"time": clock(reg.get("R16493")), "temp": tenth("R16494")},
                {"time": clock(reg.get("R16495")), "temp": tenth("R16496")},
            ],
        },
        "settings": {
            "comfort_summer": reg["R8684"] / 100 if "R8684" in reg else None,
            "comfort_winter": reg["R8688"] / 100 if "R8688" in reg else None,
            "humidity_summer": reg.get("R8660"),
            "humidity_winter": reg.get("R8661"),
            "heating_water": [
                {"time": clock(t), "temp": tenth(c)}
                for t, c in ((0, "R16444"), (reg.get("R16445"), "R16446"), (reg.get("R16447"), "R16448"), (reg.get("R16449"), "R16450"))
            ],
            "cooling_water": [
                {"time": clock(t), "temp": tenth(c)}
                for t, c in ((0, "R16451"), (reg.get("R16452"), "R16453"), (reg.get("R16454"), "R16455"), (reg.get("R16456"), "R16457"))
            ],
        },
        "alarms": [name for code, name in ALARMS.items() if reg.get(code)],
    }
