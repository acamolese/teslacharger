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
    AUTO = "auto"  # di giorno segue il sole, di notte lascia fare a Octopus
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
    deficit_count: int = 0
    # Ultimo dato Solax già valutato, per non contare due volte la stessa lettura
    last_data_time: str | None = None
    # Ultimo risveglio dell'auto e ultima volta che il cavo risultava scollegato
    last_wake: datetime | None = None
    unplugged_at: datetime | None = None

    def to_json(self) -> dict:
        data = asdict(self)
        for key in ("last_switch", "last_wake", "unplugged_at"):
            data[key] = data[key].isoformat() if data[key] else None
        return data

    @classmethod
    def from_json(cls, data: dict) -> "ControlState":
        data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for key in ("last_switch", "last_wake", "unplugged_at"):
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


def _min_power(settings: Settings) -> int:
    return settings.min_amps * NOMINAL_VOLTAGE


def precheck(
    now: datetime,
    mode: Mode,
    plant: PlantSnapshot,
    boosting: bool,
    state: ControlState,
    settings: Settings,
) -> Decision | None:
    """Decide senza interrogare l'auto, quando i suoi dati non servono.

    Restituisce None se per decidere bisogna leggere lo stato dell'auto.
    Evita letture inutili, che Tesla fa pagare e che tengono sveglia l'auto.
    """
    if mode is Mode.OFF and not (boosting and state.started_by_us):
        return Decision(Action.HOLD, "sistema in pausa", replace(state, started_by_us=False))
    if mode is not Mode.AUTO or boosting:
        if boosting and not state.started_by_us and mode is Mode.AUTO:
            return Decision(Action.HOLD, "carica immediata avviata manualmente", state)
        return None

    state = replace(state, started_by_us=False, deficit_count=0)
    if not _in_day(now, settings):
        return Decision(Action.HOLD, "fuori dalla fascia diurna, la notte è gestita da Octopus", state)
    if plant.battery_soc < settings.soc_start:
        return Decision(
            Action.HOLD,
            f"precedenza alla batteria di casa ({plant.battery_soc}%, avvio dall'{settings.soc_start}%)",
            state,
        )
    if plant.excess_w < _min_power(settings):
        return Decision(
            Action.HOLD,
            f"surplus {plant.excess_w:.0f} W, ne servono almeno {_min_power(settings)}",
            state,
        )
    if state.unplugged_at and now - state.unplugged_at < timedelta(minutes=settings.car_retry_minutes):
        return Decision(Action.HOLD, "c'è surplus ma il cavo non risultava collegato", state)
    return None


def decide(
    now: datetime,
    mode: Mode,
    plant: PlantSnapshot,
    car: CarStatus | None,
    boosting: bool,
    state: ControlState,
    settings: Settings,
) -> Decision:
    fresh = plant.data_time != state.last_data_time
    state = replace(state, last_data_time=plant.data_time)
    early = precheck(now, mode, plant, boosting, state, settings)
    if early is not None:
        return early

    if car is None:
        # L'auto è in standby: va svegliata per sapere se è collegata
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
            replace(state, started_by_us=False, last_switch=now, deficit_count=0),
            amps=car.max_amps,
        )

    if mode is Mode.OFF:
        return stop("sistema messo in pausa")

    if not car.plugged:
        state = replace(state, unplugged_at=now)
        if ours:
            return stop("cavo scollegato")
        return Decision(Action.HOLD, "cavo non collegato", state)
    state = replace(state, unplugged_at=None)

    if car.level >= car.limit:
        if ours:
            return stop(f"auto al {car.level}%, carica completata")
        return Decision(Action.HOLD, f"auto già al {car.level}%", state)

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

    # Modalità automatica. Nel surplus letto da Solax è già compreso il consumo dell'auto:
    # lo si aggiunge di nuovo per sapere quanta potenza le si può destinare in tutto.
    voltage = car.voltage if car.charging and car.voltage > 100 else NOMINAL_VOLTAGE
    available = plant.excess_w + car.power_w + settings.battery_assist_w
    target = min(car.max_amps, int(available // voltage))

    if not boosting:
        if target < settings.min_amps:
            return Decision(Action.HOLD, f"surplus {plant.excess_w:.0f} W insufficiente", state)
        if not can_switch:
            return Decision(Action.HOLD, "surplus sufficiente, attesa tra due manovre", state)
        return Decision(
            Action.START,
            f"surplus {plant.excess_w:.0f} W, batteria di casa al {plant.battery_soc}%",
            replace(state, started_by_us=True, last_switch=now),
            amps=target,
        )

    if not _in_day(now, settings):
        return stop("fine della fascia diurna")
    if plant.battery_soc < settings.soc_stop:
        return stop(f"batteria di casa al {plant.battery_soc}%, sotto la soglia di stop {settings.soc_stop}%")
    if not fresh:
        return Decision(Action.HOLD, f"carica solare in corso a {car.amps} A", state)

    if target < settings.min_amps:
        state = replace(state, deficit_count=state.deficit_count + 1)
        if state.deficit_count >= settings.deficit_samples and can_switch:
            return stop(f"sole insufficiente per {state.deficit_count} letture consecutive")
        target = settings.min_amps
    else:
        state = replace(state, deficit_count=0)

    if target != car.amps:
        return Decision(Action.SET_AMPS, f"potenza disponibile {available:.0f} W", state, amps=target)
    return Decision(Action.HOLD, f"carica solare in corso a {car.amps} A", state)
