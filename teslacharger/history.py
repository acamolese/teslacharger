"""Storico su SQLite: letture dell'impianto, stato dell'auto ed energia caricata."""

import sqlite3
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plant (
    time TEXT PRIMARY KEY, pv_w REAL, grid_w REAL, battery_w REAL, soc INTEGER
);
CREATE TABLE IF NOT EXISTS car (
    time TEXT PRIMARY KEY, level INTEGER, range_km REAL, odometer_km REAL, charging INTEGER
);
CREATE TABLE IF NOT EXISTS dispatch (
    start TEXT PRIMARY KEY, end TEXT, kwh REAL
);
"""


class History:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _run(self, sql: str, args=()) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def _all(self, sql: str, args=()) -> list:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def log_plant(self, plant) -> None:
        self._run(
            "INSERT OR IGNORE INTO plant VALUES (?, ?, ?, ?, ?)",
            (plant.data_time, plant.pv_w, plant.grid_w, plant.battery_w, plant.battery_soc),
        )

    def log_car(self, when: datetime, info: dict) -> None:
        self._run(
            "INSERT OR REPLACE INTO car VALUES (?, ?, ?, ?, ?)",
            (
                when.isoformat(timespec="seconds"),
                info.get("level"),
                info.get("range_km"),
                info.get("odometer_km"),
                int(bool(info.get("charging"))),
            ),
        )

    def log_dispatch(self, start: datetime, end: datetime, kwh: float) -> None:
        """Registra un'ora di carica consuntivata da Octopus (ora locale)."""
        self._run(
            "INSERT OR REPLACE INTO dispatch VALUES (?, ?, ?)",
            (start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"), kwh),
        )

    def _non_solar_kw(self, start: str, end: str) -> float | None:
        """Potenza media presa da batteria di casa e rete nell'intervallo, dai dati Solax."""
        rows = self._all(
            "SELECT battery_w, grid_w FROM plant WHERE time >= ? AND time < ?",
            (start.replace("T", " "), end.replace("T", " ")),
        )
        if not rows:
            return None
        # Batteria negativa = si scarica. Rete negativa = prelievo (segno da confermare).
        return sum(max(0.0, -b) + max(0.0, -g) for b, g in rows) / len(rows) / 1000

    def daily(self, days: int, day_start: time, day_end: time) -> list[dict]:
        """Energia caricata per giorno, divisa tra sole, altro di giorno e notte."""
        first = (datetime.now() - timedelta(days=days - 1)).date()
        result = {
            (first + timedelta(days=i)).isoformat(): {"sun": 0.0, "day_other": 0.0, "night": 0.0}
            for i in range(days)
        }
        for start, end, kwh in self._all("SELECT start, end, kwh FROM dispatch WHERE start >= ?", (first.isoformat(),)):
            begin = datetime.fromisoformat(start)
            day = result.get(begin.date().isoformat())
            if day is None:
                continue
            if not day_start <= begin.time() < day_end:
                day["night"] += kwh
                continue
            hours = max((datetime.fromisoformat(end) - begin).total_seconds() / 3600, 1 / 60)
            non_solar = self._non_solar_kw(start, end)
            other = kwh if non_solar is None else min(kwh, non_solar * hours)
            day["day_other"] += other
            day["sun"] += kwh - other
        return [{"day": d, **{k: round(v, 2) for k, v in e.items()}} for d, e in result.items()]

    def last_car(self) -> dict | None:
        rows = self._all("SELECT time, level, range_km, odometer_km FROM car ORDER BY time DESC LIMIT 1")
        if not rows:
            return None
        return dict(zip(("time", "level", "range_km", "odometer_km"), rows[0]))

    def consumption(self) -> dict | None:
        """Stima dei consumi di guida dai tratti in cui la batteria è scesa e i km sono saliti."""
        rows = self._all("SELECT level, odometer_km FROM car WHERE odometer_km IS NOT NULL ORDER BY time")
        km = percent = 0.0
        for (level_a, odo_a), (level_b, odo_b) in zip(rows, rows[1:]):
            if odo_b - odo_a > 1 and level_a > level_b:
                km += odo_b - odo_a
                percent += level_a - level_b
        if km < 50:
            return None
        return {"km": round(km), "percent": round(percent)}
