"""Storico su SQLite: letture dell'impianto, stato dell'auto ed energia caricata."""

import sqlite3
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

# Chilometri registrati oltre i quali la stima dei consumi è attendibile
MIN_KM = 50
# Soglie oltre le quali gli altri indicatori sono attendibili
MIN_POINTS = 10  # punti percentuali di batteria caricati sotto osservazione
MIN_WALL_KWH = 2  # energia prelevata dalla presa sotto osservazione
MIN_IDLE_HOURS = 12  # ore di sosta tra due letture

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
CREATE TABLE IF NOT EXISTS session (
    start TEXT PRIMARY KEY, end TEXT, type TEXT, kwh REAL, soc_change REAL, soc_final REAL, problems TEXT
);
CREATE TABLE IF NOT EXISTS supercharge (
    id TEXT PRIMARY KEY, start TEXT, end TEXT, site TEXT, kwh REAL, cost REAL
);
CREATE TABLE IF NOT EXISTS plug (
    time TEXT PRIMARY KEY, plugged INTEGER, level INTEGER
);
CREATE TABLE IF NOT EXISTS evening (
    day TEXT PRIMARY KEY, asked_at TEXT, suggestion TEXT, choice TEXT, by_user INTEGER, home_kwh REAL, level INTEGER
);
CREATE TABLE IF NOT EXISTS event (
    time TEXT, icon TEXT, title TEXT, sub TEXT
);
"""
# Colonne aggiunte dopo la prima versione, per tabella
ADDED_COLUMNS = {
    "car": ("energy_added REAL", "voltage REAL", "amps REAL"),
    # Potenza in uscita dall'inverter, stato dell'auto per Octopus, modalità del sistema,
    # batteria di casa tenuta a riposo: servono per ricostruire a posteriori ogni intervallo
    "plant": ("ac_w REAL", "octopus TEXT", "mode TEXT", "held INTEGER"),
}


class History:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            present = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column.split()[0] not in present:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        self._db.commit()
        self._lock = threading.Lock()

    def _run(self, sql: str, args=()) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def _all(self, sql: str, args=()) -> list:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def log_plant(self, plant, octopus: str | None = None, mode: str | None = None, held: bool = False) -> None:
        """Registra una lettura dell'impianto con il contesto in cui è avvenuta."""
        self._run(
            "INSERT OR IGNORE INTO plant (time, pv_w, grid_w, battery_w, soc, ac_w, octopus, mode, held)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plant.data_time, plant.pv_w, plant.grid_w, plant.battery_w, plant.battery_soc,
                plant.inverter_ac_w, octopus, mode, int(held),
            ),
        )

    def log_session(self, session: dict) -> None:
        """Registra una sessione di ricarica come la vede Octopus (ora locale)."""
        self._run(
            "INSERT OR REPLACE INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session["start"], session["end"], session["type"], session["kwh"],
                session["soc_change"], session["soc_final"], session["problems"],
            ),
        )

    def log_supercharge(self, s: dict) -> None:
        self._run(
            "INSERT OR REPLACE INTO supercharge VALUES (?, ?, ?, ?, ?, ?)",
            (s["id"], s["start"], s["end"], s["site"], s["kwh"], s["cost"]),
        )

    def supercharges(self, limit: int = 12) -> list[dict]:
        rows = self._all("SELECT start, end, site, kwh, cost FROM supercharge ORDER BY start DESC LIMIT ?", (limit,))
        return [dict(zip(("start", "end", "site", "kwh", "cost"), row)) for row in rows]

    def supercharge_month(self, month: str) -> dict:
        row = self._all("SELECT COALESCE(SUM(kwh), 0), COALESCE(SUM(cost), 0), COUNT(*) FROM supercharge WHERE start LIKE ?", (month + "%",))[0]
        return {"kwh": round(row[0], 1), "cost": round(row[1], 2), "count": row[2]}

    def km_since(self, day: str) -> float | None:
        """Chilometri percorsi dalla prima lettura del periodo all'ultima."""
        rows = self._all("SELECT MIN(odometer_km), MAX(odometer_km) FROM car WHERE time >= ? AND odometer_km IS NOT NULL", (day,))
        return round(rows[0][1] - rows[0][0]) if rows and rows[0][0] is not None else None

    def log_plug(self, when: datetime, plugged: bool, level: int | None) -> None:
        """Registra quando l'auto viene collegata o scollegata: servono per capire le abitudini."""
        self._run("INSERT OR REPLACE INTO plug VALUES (?, ?, ?)", (when.isoformat(timespec="seconds"), int(plugged), level))

    def log_evening(self, q: dict) -> None:
        self._run(
            "INSERT OR REPLACE INTO evening VALUES (?, ?, ?, ?, ?, ?, ?)",
            (q["day"], q["asked_at"], q["suggestion"], q.get("choice"), int(bool(q.get("by_user"))), q.get("home_kwh"), q.get("level")),
        )

    def log_event(self, when: str, icon: str, title: str, sub: str) -> None:
        self._run("INSERT INTO event VALUES (?, ?, ?, ?)", (when, icon, title, sub))

    def log_car(self, when: datetime, info: dict) -> None:
        self._run(
            "INSERT OR REPLACE INTO car (time, level, range_km, odometer_km, charging, energy_added, voltage, amps)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                when.isoformat(timespec="seconds"),
                info.get("level"),
                info.get("range_km"),
                info.get("odometer_km"),
                int(bool(info.get("charging"))),
                info.get("energy_added_kwh"),
                info.get("voltage"),
                info.get("amps"),
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

    def since(self) -> str | None:
        """Primo giorno per cui esistono dati."""
        rows = self._all("SELECT MIN(time) FROM plant")
        return rows[0][0][:10] if rows and rows[0][0] else None

    def consumption(self) -> dict:
        """Chilometri e batteria usata nei tratti di guida registrati, per stimare i consumi."""
        rows = self._all("SELECT level, odometer_km FROM car WHERE odometer_km IS NOT NULL ORDER BY time")
        km = percent = 0.0
        for (level_a, odo_a), (level_b, odo_b) in zip(rows, rows[1:]):
            if odo_b - odo_a > 1 and level_a > level_b:
                km += odo_b - odo_a
                percent += level_a - level_b
        return {"km": round(km), "percent": round(percent), "ready": km >= MIN_KM}

    def insights(self) -> dict:
        """Indicatori ricavati dalle letture dell'auto. Ogni voce è None finché i dati non bastano."""
        rows = self._all(
            "SELECT time, level, range_km, odometer_km, charging, energy_added, voltage, amps"
            " FROM car WHERE level IS NOT NULL ORDER BY time"
        )
        added = gained = wall_kwh = wall_added = 0.0
        idle_drop = idle_hours = 0.0
        for a, b in zip(rows, rows[1:]):
            hours = (datetime.fromisoformat(b[0]) - datetime.fromisoformat(a[0])).total_seconds() / 3600
            if hours <= 0:
                continue
            both_charging = a[4] and b[4] and a[5] is not None and b[5] is not None
            if both_charging and hours < 0.34 and b[5] >= a[5] and b[1] >= a[1]:
                # Due letture della stessa carica: energia entrata in batteria e punti guadagnati
                added += b[5] - a[5]
                gained += b[1] - a[1]
                if a[6] and a[7] and b[6] and b[7]:
                    wall_kwh += (a[6] * a[7] + b[6] * b[7]) / 2 * hours / 1000
                    wall_added += b[5] - a[5]
            parked = a[3] is not None and b[3] is not None and abs(b[3] - a[3]) < 0.5
            if parked and not a[4] and not b[4] and 1 <= hours <= 72 and b[1] <= a[1]:
                idle_drop += a[1] - b[1]
                idle_hours += hours

        ranges = [(r[0], r[2] / r[1] * 100) for r in rows if r[2] and r[1] >= 20]
        last_charge = next((r for r in reversed(rows) if r[4] and r[6] and r[7]), None)
        return {
            "capacity_kwh": round(added / gained * 100, 1) if gained >= MIN_POINTS else None,
            "capacity_points": round(gained),
            "capacity_points_needed": MIN_POINTS,
            "full_range_km": round(ranges[-1][1]) if ranges else None,
            "full_range_first_km": round(ranges[0][1]) if len(ranges) > 1 else None,
            "full_range_since": ranges[0][0][:10] if len(ranges) > 1 else None,
            "efficiency": round(wall_added / wall_kwh * 100) if wall_kwh >= MIN_WALL_KWH else None,
            "efficiency_kwh": round(wall_kwh, 1),
            "efficiency_kwh_needed": MIN_WALL_KWH,
            "plug_voltage": round(last_charge[6]) if last_charge else None,
            "plug_amps": round(last_charge[7]) if last_charge else None,
            "plug_time": last_charge[0] if last_charge else None,
            "idle_percent_per_day": round(idle_drop / idle_hours * 24, 1) if idle_hours >= MIN_IDLE_HOURS else None,
            "idle_hours": round(idle_hours),
            "idle_hours_needed": MIN_IDLE_HOURS,
        }
