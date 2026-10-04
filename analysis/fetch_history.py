"""Scarica da Solax lo storico a 5 minuti di inverter e batteria e lo salva in data/solax_history.db.

L'API concede al massimo 12 ore per chiamata: un anno richiede circa 800 chiamate.
Lo script riprende da dove si era fermato.

Uso: python3 -m analysis.fetch_history 2025-10-01 2026-10-01
"""

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from teslacharger.config import load_env
from teslacharger.solax import DEVICE_BATTERY, DEVICE_INVERTER, SUCCESS, SolaxClient

WINDOW = timedelta(hours=12)
# Si resta ben sotto il limite di chiamate al minuto, che vale anche per il servizio in funzione
WORKERS = 2
PAUSE_SECONDS = 2
RATE_LIMITED = 10406


def main() -> None:
    load_env()
    start, end = (datetime.fromisoformat(a) for a in sys.argv[1:3])
    db = sqlite3.connect("data/solax_history.db")
    db.executescript(
        """CREATE TABLE IF NOT EXISTS inverter (time TEXT PRIMARY KEY, pv_w REAL, ac_w REAL, grid_w REAL);
           CREATE TABLE IF NOT EXISTS battery (time TEXT PRIMARY KEY, power_w REAL, soc REAL);
           CREATE TABLE IF NOT EXISTS done (kind INTEGER, start TEXT, PRIMARY KEY (kind, start));"""
    )
    client = SolaxClient()
    sns = client._device_sns()

    def fetch(job):
        kind, cursor = job
        begin = int(cursor.timestamp())
        stop_ts = min(int(end.timestamp()), begin + int(WINDOW.total_seconds()))
        for attempt in range(8):
            data = client._request(
                "/openapi/v2/device/history_data",
                params={
                    "snList": sns[kind], "deviceType": kind, "businessType": 1,
                    "startTime": begin * 1000, "endTime": stop_ts * 1000,
                    "timeInterval": 5,
                },
            )
            if data.get("code") == SUCCESS:
                time.sleep(PAUSE_SECONDS)
                return job, data.get("result") or []
            # Limite di chiamate raggiunto: si aspetta che passi il minuto
            time.sleep(65 if data.get("code") == RATE_LIMITED else 10 * (attempt + 1))
        raise RuntimeError(f"interrotto a {cursor}: {data}")

    # Lo storico della batteria serve solo per ricavarne i limiti: basta una finestra su dodici
    jobs, cursor, index = [], start, 0
    while cursor < end:
        for kind in (DEVICE_INVERTER, DEVICE_BATTERY):
            if kind == DEVICE_BATTERY and index % 12:
                continue
            if not db.execute("SELECT 1 FROM done WHERE kind = ? AND start = ?", (kind, cursor.isoformat())).fetchone():
                jobs.append((kind, cursor))
        # Avanza di dodici ore effettive, anche a cavallo del cambio d'ora
        cursor = datetime.fromtimestamp(cursor.timestamp() + WINDOW.total_seconds())
        index += 1
    calls = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for (kind, cursor), rows in pool.map(fetch, jobs):
            for row in rows:
                if kind == DEVICE_INVERTER:
                    db.execute(
                        "INSERT OR REPLACE INTO inverter VALUES (?, ?, ?, ?)",
                        (row["plantLocalTime"], row.get("MPPTTotalInputPower") or 0, row.get("acPower1") or 0, row.get("gridPower") or 0),
                    )
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO battery VALUES (?, ?, ?)",
                        (row["plantLocalTime"], row.get("chargeDischargePower") or 0, row.get("batterySOC")),
                    )
            db.execute("INSERT OR REPLACE INTO done VALUES (?, ?)", (kind, cursor.isoformat()))
            calls += 1
            if calls % 20 == 0:
                db.commit()
    db.commit()
    counts = [db.execute(f"SELECT COUNT(*), MIN(time), MAX(time) FROM {t}").fetchone() for t in ("inverter", "battery")]
    print("chiamate:", calls, "| inverter:", counts[0], "| batteria:", counts[1])


if __name__ == "__main__":
    main()
