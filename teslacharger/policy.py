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
    BOOST = "boost"  # carica subito alla massima corrente, senza guardare sole né orari
    SOLAR = "solar"  # di giorno carica con una quota dei pannelli
    AUTO = "auto"  # il sistema non interviene: la carica è quella notturna di Octopus


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
    # Letture consecutive con sole insufficiente durante una carica
    deficit_count: int = 0
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
    # True quando manca il sole e serve il consenso dell'utente per usare rete o batteria
    ask: bool = False


NO_SUN = "sole insufficiente: serve la tua autorizzazione per caricare da rete o batteria di casa"


def _in_day(now: datetime, settings: Settings) -> bool:
    return settings.day_start <= now.time() < settings.day_end


def share_amps(plant: PlantSnapshot, settings: Settings, voltage: int = NOMINAL_VOLTAGE) -> int:
    """Ampere corrispondenti alla quota dei pannelli destinata all'auto."""
    return int(plant.pv_w * settings.pv_share / 100 // voltage)


def precheck(
    now: datetime,
    mode: Mode,
    plant: PlantSnapshot,
    boosting: bool,
    plugged: bool,
    grid_ok: bool,
    state: ControlState,
    settings: Settings,
) -> Decision | None:
    """Decide senza interrogare l'auto, quando i suoi dati non servono.

    Restituisce None se per decidere bisogna leggere lo stato dell'auto.
    Evita letture inutili, che Tesla fa pagare e che tengono sveglia l'auto.
    `plugged` è l'indicazione di Octopus sulla presenza del cavo, `grid_ok` il consenso
    dell'utente a caricare da rete o batteria di casa quando il sole non basta.
    """
    ours = boosting and state.started_by_us
    if not boosting:
        state = replace(state, started_by_us=False)

    if mode is Mode.AUTO:
        return None if ours else Decision(Action.HOLD, "carica notturna affidata a Octopus", state)
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
    if not grid_ok and share_amps(plant, settings) < settings.min_amps:
        return Decision(Action.HOLD, NO_SUN, state, ask=True)
    return None


def decide(
    now: datetime,
    mode: Mode,
    plant: PlantSnapshot,
    car: CarStatus | None,
    boosting: bool,
    plugged: bool,
    grid_ok: bool,
    state: ControlState,
    settings: Settings,
) -> Decision:
    early = precheck(now, mode, plant, boosting, plugged, grid_ok, state, settings)
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

    # Corrente massima: il limite scelto, mai oltre quello del cavo
    cap = min(car.max_amps, settings.max_amps)

    def stop(reason: str, ask: bool = False) -> Decision:
        return Decision(
            Action.STOP,
            reason,
            replace(state, started_by_us=False, last_switch=now, deficit_count=0),
            amps=cap,
            ask=ask,
        )

    if mode is Mode.AUTO:
        return stop("passaggio alla carica notturna di Octopus")

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
                Action.START, "carica subito alla massima corrente",
                replace(state, last_switch=now), amps=cap,
            )
        if car.amps != cap:
            return Decision(Action.SET_AMPS, "carica subito alla massima corrente", state, amps=cap)
        return Decision(Action.HOLD, f"carica immediata in corso a {car.amps} A", state)

    # Carica col sole
    if ours and not _in_day(now, settings):
        return stop("fine della fascia diurna, la notte è gestita da Octopus")

    # All'auto va una quota della produzione dei pannelli. Se la quota non arriva alla
    # corrente minima, si carica al minimo solo con il consenso dell'utente.
    voltage = car.voltage if car.charging and car.voltage > 100 else NOMINAL_VOLTAGE
    share = share_amps(plant, settings, voltage)
    floor = min(settings.min_amps, cap)
    enough_sun = share >= floor
    target = max(floor, min(cap, share))
    detail = f"pannelli a {plant.pv_w:.0f} W, all'auto fino al {settings.pv_share}%"
    if not enough_sun:
        detail = f"pannelli a {plant.pv_w:.0f} W, carica al minimo da rete o batteria autorizzata"

    if not boosting:
        if not enough_sun and not grid_ok:
            return Decision(Action.HOLD, NO_SUN, state, ask=True)
        if not can_switch:
            return Decision(Action.HOLD, "attesa tra due manovre", state)
        return Decision(
            Action.START, detail, replace(state, started_by_us=True, last_switch=now), amps=target
        )

    if not enough_sun and not grid_ok:
        # Una nuvola non deve fermare la carica: si aspetta qualche lettura al minimo
        state = replace(state, deficit_count=state.deficit_count + 1)
        if state.deficit_count >= settings.deficit_samples and can_switch:
            return stop(NO_SUN, ask=True)
        detail = f"pannelli a {plant.pv_w:.0f} W, sole in calo"
    else:
        state = replace(state, deficit_count=0)
    if target != car.amps:
        return Decision(Action.SET_AMPS, detail, state, amps=target)
    return Decision(Action.HOLD, f"carica diurna in corso a {car.amps} A", state)
