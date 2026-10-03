"""Regole di decisione: quando caricare l'auto e a quanti ampere.

Funzioni pure, senza chiamate di rete, così si possono provare con dati finti.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from .config import Settings
from .solax import PlantSnapshot

NOMINAL_VOLTAGE = 230


class Mode(Enum):
    AUTO = "auto"  # di giorno carica seguendo il sole, di notte lascia fare a Octopus
    BOOST = "boost"  # carica subito alla massima potenza
    OFF = "off"  # il sistema non interviene


class Action(Enum):
    START = "avvia"
    STOP = "ferma"
    SET_AMPS = "regola"
    WAKE = "sveglia"
    HOLD = "nessuna azione"


@dataclass(frozen=True)
class CarStatus:
    plugged: bool
    charging: bool
    level: int
    limit: int
    amps: int
    max_amps: int
    voltage: int

    @property
    def power_w(self) -> float:
        return self.amps * self.voltage if self.charging else 0.0


@dataclass(frozen=True)
class ControlState:
    # True se la carica in corso è stata avviata da questo sistema
    started_by_us: bool = False
    last_switch: datetime | None = None
    # Ultimo dato Solax già valutato, per non agire due volte sulla stessa lettura
    last_data_time: str | None = None
    # Ultimo risveglio dell'auto
    last_wake: datetime | None = None
    # Ultima volta che l'auto risultava scollegata o già carica: per un po' non la si interroga
    idle_at: datetime | None = None

    def to_json(self) -> dict:
        data = asdict(self)
        for key in ("last_switch", "last_wake", "idle_at"):
            data[key] = data[key].isoformat() if data[key] else None
        return data

    @classmethod
    def from_json(cls, data: dict) -> "ControlState":
        data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for key in ("last_switch", "last_wake", "idle_at"):
            if data.get(key):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    state: ControlState
    amps: int | None = None


def _in_day(now: datetime, settings: Settings) -> bool:
    return settings.day_start <= now.time() < settings.day_end


def precheck(
    now: datetime,
    mode: Mode,
    plant: PlantSnapshot,
    boosting: bool,
    plugged: bool,
    state: ControlState,
    settings: Settings,
) -> Decision | None:
    """Decide senza interrogare l'auto, quando i suoi dati non servono.

    Restituisce None se per decidere bisogna leggere lo stato dell'auto.
    Evita letture inutili, che Tesla fa pagare e che tengono sveglia l'auto.
    `plugged` è l'indicazione di Octopus sulla presenza del cavo.
    """
    ours = boosting and state.started_by_us
    if not boosting:
        state = replace(state, started_by_us=False)

    if mode is Mode.OFF:
        return None if ours else Decision(Action.HOLD, "sistema in pausa", state)
    if mode is Mode.BOOST:
        return None
    if boosting and not ours:
        return Decision(Action.HOLD, "carica immediata avviata manualmente", state)

    if not _in_day(now, settings):
        if ours:
            return None
        return Decision(Action.HOLD, "fuori dalla fascia diurna, la notte è gestita da Octopus", state)
    if ours:
        if plant.data_time == state.last_data_time:
            return Decision(Action.HOLD, "carica diurna in corso, in attesa di nuovi dati dai pannelli", state)
        return None
    if not plugged:
        return Decision(Action.HOLD, "auto non collegata", state)
    if state.idle_at and now - state.idle_at < timedelta(minutes=settings.car_retry_minutes):
        return Decision(Action.HOLD, "auto già carica o cavo scollegato all'ultimo controllo", state)
    return None


def target_amps(plant: PlantSnapshot, car: CarStatus, settings: Settings) -> int:
    """Corrente da destinare all'auto: una quota dei pannelli, mai sotto la base."""
    voltage = car.voltage if car.charging and car.voltage > 100 else NOMINAL_VOLTAGE
    share_w = plant.pv_w * settings.pv_share / 100
    return max(min(settings.min_amps, car.max_amps), min(car.max_amps, int(share_w // voltage)))


def decide(
    now: datetime,
    mode: Mode,
    plant: PlantSnapshot,
    car: CarStatus | None,
    boosting: bool,
    plugged: bool,
    state: ControlState,
    settings: Settings,
) -> Decision:
    early = precheck(now, mode, plant, boosting, plugged, state, settings)
    state = replace(state, last_data_time=plant.data_time)
    if early is not None:
        return replace(early, state=replace(early.state, last_data_time=plant.data_time))

    if car is None:
        # L'auto è in standby: va svegliata per poterla leggere e comandare
        if state.last_wake and now - state.last_wake < timedelta(minutes=settings.car_retry_minutes):
            return Decision(Action.HOLD, "auto in standby, risveglio già tentato da poco", state)
        return Decision(Action.WAKE, "auto in standby", replace(state, last_wake=now))

    ours = boosting and state.started_by_us
    can_switch = state.last_switch is None or now - state.last_switch >= timedelta(
        minutes=settings.min_switch_minutes
    )

    def stop(reason: str) -> Decision:
        return Decision(
            Action.STOP,
            reason,
            replace(state, started_by_us=False, last_switch=now),
            amps=car.max_amps,
        )

    if mode is Mode.OFF:
        return stop("sistema messo in pausa")

    if not car.plugged:
        state = replace(state, idle_at=now)
        return stop("cavo scollegato") if ours else Decision(Action.HOLD, "cavo non collegato", state)
    if car.level >= car.limit:
        state = replace(state, idle_at=now)
        if ours:
            return stop(f"auto al {car.level}%, carica completata")
        return Decision(Action.HOLD, f"auto già al {car.level}%", state)
    state = replace(state, idle_at=None)

    if mode is Mode.BOOST:
        state = replace(state, started_by_us=True)
        if not boosting:
            return Decision(
                Action.START, "carica subito alla massima potenza",
                replace(state, last_switch=now), amps=car.max_amps,
            )
        if car.amps < car.max_amps:
            return Decision(Action.SET_AMPS, "carica subito alla massima potenza", state, amps=car.max_amps)
        return Decision(Action.HOLD, f"carica immediata in corso a {car.amps} A", state)

    # Modalità automatica
    if ours and not _in_day(now, settings):
        return stop("fine della fascia diurna, la notte è gestita da Octopus")

    target = target_amps(plant, car, settings)
    detail = f"pannelli a {plant.pv_w:.0f} W, all'auto fino al {settings.pv_share}%"
    if not boosting:
        if not can_switch:
            return Decision(Action.HOLD, "attesa tra due manovre", state)
        return Decision(
            Action.START, detail, replace(state, started_by_us=True, last_switch=now), amps=target
        )
    if target != car.amps:
        return Decision(Action.SET_AMPS, detail, state, amps=target)
    return Decision(Action.HOLD, f"carica diurna in corso a {car.amps} A", state)
