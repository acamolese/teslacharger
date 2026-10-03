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
            live=os.environ.get("LIVE", "").lower() in ("1", "true", "si", "sì"),
        )
