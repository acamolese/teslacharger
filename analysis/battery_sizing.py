"""Stima di quanto farebbe risparmiare una batteria di casa più grande.

Parte dallo storico reale a 5 minuti (data/solax_history.db, creato da fetch_history)
e rifà i conti dell'anno con batterie di capacità diversa: stessa produzione, stessi
consumi, cambia solo quanta energia si riesce a spostare dal giorno alla sera.

Uso: python3 -m analysis.battery_sizing
"""

import sqlite3
from collections import defaultdict
from datetime import datetime

# Batteria attuale: due moduli da 5,76 kWh, con la salute dichiarata dall'inverter
MODULE_KWH = 5.76
CURRENT_MODULES = 2
HEALTH = 0.96
# Resa andata e ritorno misurata sui totali di sempre della batteria
ROUND_TRIP = 0.86
INVERTER_EFFICIENCY = 0.97
MAX_GAP_HOURS = 0.25
INVERTER_MAX_W = 6000
PRICE_KWH = 0.229
EXPORT_PRICES = (0.0, 0.05, 0.10)


def load_series() -> list[tuple]:
    db = sqlite3.connect("data/solax_history.db")
    rows = db.execute("SELECT time, pv_w, ac_w, grid_w FROM inverter ORDER BY time").fetchall()
    series, previous = [], None
    for stamp, pv, ac, grid in rows:
        now = datetime.fromisoformat(stamp)
        if previous is not None:
            hours = min((now - previous).total_seconds() / 3600, MAX_GAP_HOURS)
            # Consumo di casa: quello che esce dall'inverter meno quello che va in rete
            series.append((stamp[:7], hours, pv, max(0.0, ac - grid), grid))
        previous = now
    return series


def battery_limits() -> tuple[float, float]:
    """Carica minima a cui la batteria scende e potenza massima che ha erogato, dai dati reali."""
    db = sqlite3.connect("data/solax_history.db")
    low = db.execute("SELECT soc FROM battery WHERE soc > 0 ORDER BY soc LIMIT 1 OFFSET 50").fetchone()
    power = db.execute("SELECT ABS(power_w) FROM battery ORDER BY ABS(power_w) DESC LIMIT 1 OFFSET 50").fetchone()
    return (low[0] if low else 10) / 100, (power[0] if power else 3000)


def simulate(series, capacity_kwh: float, min_soc: float, max_power_w: float, pv_scale: float = 1.0) -> dict:
    one_way = ROUND_TRIP ** 0.5
    floor = capacity_kwh * min_soc
    stored = floor
    months = defaultdict(lambda: {"imported": 0.0, "exported": 0.0})
    for month, hours, pv, load, _ in series:
        # Con più pannelli la produzione cresce in proporzione, fino al limite dell'inverter
        net = min(pv * pv_scale * INVERTER_EFFICIENCY, INVERTER_MAX_W + max_power_w) - load
        if net >= 0:
            charge = min(net, max_power_w, (capacity_kwh - stored) / one_way / hours * 1000 if hours else 0)
            stored += charge * one_way * hours / 1000
            months[month]["exported"] += (net - charge) * hours / 1000
        else:
            discharge = min(-net, max_power_w, (stored - floor) * one_way / hours * 1000 if hours else 0)
            stored -= discharge / one_way * hours / 1000
            months[month]["imported"] += (-net - discharge) * hours / 1000
    return months


def total(months, key) -> float:
    return sum(m[key] for m in months.values())


def main() -> None:
    series = load_series()
    min_soc, max_power = battery_limits()
    hours = sum(s[1] for s in series)
    actual_import = sum(max(0.0, -s[4]) * s[1] for s in series) / 1000
    actual_export = sum(max(0.0, s[4]) * s[1] for s in series) / 1000
    pv = sum(s[2] * s[1] for s in series) / 1000
    load = sum(s[3] * s[1] for s in series) / 1000
    print(f"Dati: {len(series)} letture, {hours / 24:.0f} giorni coperti, da {series[0][0]} a {series[-1][0]}")
    print(f"Reale: prodotti {pv:.0f} kWh, consumati {load:.0f}, prelevati {actual_import:.0f}, ceduti {actual_export:.0f}")
    print(f"Batteria: scende fino al {min_soc:.0%}, potenza massima {max_power / 1000:.1f} kW\n")

    current = CURRENT_MODULES * MODULE_KWH * HEALTH
    base = simulate(series, current, min_soc, max_power)
    print(
        f"Simulazione con la batteria attuale ({current:.1f} kWh): prelevati {total(base, 'imported'):.0f}, "
        f"ceduti {total(base, 'exported'):.0f}  (controllo: il reale è {actual_import:.0f} e {actual_export:.0f})\n"
    )
    scale = 365 * 24 / hours
    print("Moduli  Capacità  Prelievo in meno  Cessione in meno  Risparmio annuo con energia ceduta pagata 0 / 5 / 10 cent")
    for extra in (1, 2, 3, 6):
        capacity = current + extra * MODULE_KWH
        result = simulate(series, capacity, min_soc, max_power)
        less_import = (total(base, "imported") - total(result, "imported")) * scale
        less_export = (total(base, "exported") - total(result, "exported")) * scale
        savings = " / ".join(f"{less_import * PRICE_KWH - less_export * p:5.0f} €" for p in EXPORT_PRICES)
        print(f"  +{extra}    {capacity:5.1f} kWh   {less_import:6.0f} kWh        {less_export:6.0f} kWh       {savings}")

    print("\nPrelievo evitato per mese con un modulo in più (kWh):")
    plus_one = simulate(series, current + MODULE_KWH, min_soc, max_power)
    for month in sorted(base):
        saved = base[month]["imported"] - plus_one[month]["imported"]
        print(f"  {month}: {saved:5.0f}   (prelevati {base[month]['imported']:5.0f}, ceduti {base[month]['exported']:5.0f})")


if __name__ == "__main__":
    main()
