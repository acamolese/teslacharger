"""Configurazione: credenziali dal file .env e soglie della regolazione."""

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path


def load_env(path: Path = Path(".env")) -> None:
    """Carica le variabili del file .env senza sovrascrivere quelle già presenti."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _time(name: str, default: str) -> time:
    return time.fromisoformat(os.environ.get(name, default))


def _home_plan() -> dict[int, tuple[tuple[int, int], ...]]:
    """Ore in cui di solito l'auto è a casa collegata, per giorno della settimana (lunedì = 0).

    Si legge dalla variabile HOME_PLAN, nel formato "0:9-18;2:14-19": giorno, poi una o
    più fasce orarie. Resta fuori dal codice perché descrive quando la casa è vuota.
    """
    plan = {}
    for part in os.environ.get("HOME_PLAN", "").split(";"):
        if ":" not in part:
            continue
        day, ranges = part.split(":", 1)
        plan[int(day)] = tuple(
            (int(r.split("-")[0]), int(r.split("-")[1])) for r in ranges.split(",") if "-" in r
        )
    return plan


@dataclass(frozen=True)
class Settings:
    # Corrente minima accettata dall'auto: sotto, la carica solare non è possibile
    min_amps: int
    # Corrente massima di carica: sotto il limite del cavo, per restare stabili
    max_amps: int
    # Quota della produzione dei pannelli destinata all'auto, in percentuale
    pv_share: int
    # Letture consecutive senza sole prima di fermare una carica non autorizzata dalla rete
    deficit_samples: int
    # Tempo minimo tra un avvio e uno stop (e viceversa), per non stressare l'auto
    min_switch_minutes: int
    # Fascia oraria in cui il sistema può avviare la carica solare
    day_start: time
    day_end: time
    # Intervallo tra un ciclo e l'altro (Solax aggiorna i dati ogni 5 minuti)
    poll_seconds: int
    # Attesa prima di risvegliare di nuovo l'auto o di ricontrollare il cavo
    car_retry_minutes: int
    # Costo di un kWh in più prelevato dalla rete, tasse comprese e quote fisse escluse
    price_kwh: float
    # Quote fisse mensili della bolletta (commercializzazione, trasporto, potenza, oneri), IVA compresa
    fixed_monthly: float
    # Sconto per ogni kWh caricato nelle finestre smart di Octopus
    night_discount_kwh: float
    # Livello a cui caricare di notte quando il giorno dopo l'auto resta a casa col sole
    home_day_target: int
    # Energia solare prevista per l'auto, in kWh, oltre la quale conviene aspettare il sole
    home_day_min_kwh: float
    # Ora dopo la quale, collegando l'auto, parte la domanda sulla notte, e ora in cui si decide da soli
    evening_ask_hour: int
    evening_default_hour: int
    # Senza questo interruttore il sistema scrive cosa farebbe ma non comanda l'auto
    live: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            min_amps=_int("MIN_AMPS", 5),
            max_amps=_int("MAX_AMPS", 12),
            pv_share=_int("PV_SHARE", 80),
            deficit_samples=_int("DEFICIT_SAMPLES", 2),
            min_switch_minutes=_int("MIN_SWITCH_MINUTES", 15),
            day_start=_time("DAY_START", "08:30"),
            day_end=_time("DAY_END", "19:00"),
            poll_seconds=_int("POLL_SECONDS", 150),
            car_retry_minutes=_int("CAR_RETRY_MINUTES", 30),
            price_kwh=_float("PRICE_KWH", 0.229),
            fixed_monthly=_float("FIXED_MONTHLY", 30.47),
            night_discount_kwh=_float("NIGHT_DISCOUNT_KWH", 0.036),
            home_day_target=_int("HOME_DAY_TARGET", 50),
            home_day_min_kwh=_float("HOME_DAY_MIN_KWH", 5),
            evening_ask_hour=_int("EVENING_ASK_HOUR", 18),
            evening_default_hour=_int("EVENING_DEFAULT_HOUR", 22),
            live=os.environ.get("LIVE", "").lower() in ("1", "true", "si", "sì"),
        )
