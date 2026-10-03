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


def _time(name: str, default: str) -> time:
    return time.fromisoformat(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    # Corrente minima a cui ha senso caricare l'auto
    min_amps: int
    # Quanta potenza può mettere la batteria di casa per aiutare la carica
    battery_assist_w: int
    # Sotto questa carica della batteria di casa la carica dell'auto non parte
    soc_start: int
    # Sotto questa carica della batteria di casa la carica dell'auto si ferma
    soc_stop: int
    # Letture consecutive sotto il minimo prima di fermare la carica
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
    # Senza questo interruttore il sistema scrive cosa farebbe ma non comanda l'auto
    live: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            min_amps=_int("MIN_AMPS", 5),
            battery_assist_w=_int("BATTERY_ASSIST_W", 300),
            soc_start=_int("SOC_START", 80),
            soc_stop=_int("SOC_STOP", 50),
            deficit_samples=_int("DEFICIT_SAMPLES", 2),
            min_switch_minutes=_int("MIN_SWITCH_MINUTES", 15),
            day_start=_time("DAY_START", "09:00"),
            day_end=_time("DAY_END", "18:00"),
            poll_seconds=_int("POLL_SECONDS", 150),
            car_retry_minutes=_int("CAR_RETRY_MINUTES", 30),
            live=os.environ.get("LIVE", "").lower() in ("1", "true", "si", "sì"),
        )
