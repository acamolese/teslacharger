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
def enough_sun(plant: PlantSnapshot, settings: Settings) -> bool:
    """La quota dei pannelli destinata all'auto arriva alla corrente minima?"""
    return share_amps(plant, settings) >= min(settings.min_amps, settings.max_amps)


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
    car_full: bool | None = False,
) -> Decision | None:
    """Decide senza interrogare l'auto, quando i suoi dati non servono.

    Restituisce None se per decidere bisogna leggere lo stato dell'auto.
    Evita letture inutili, che Tesla fa pagare e che tengono sveglia l'auto.
    `plugged` è l'indicazione di Octopus sulla presenza del cavo, `grid_ok` il consenso
    dell'utente a caricare da rete o batteria di casa quando il sole non basta.
    `car_full` dice se dall'ultima lettura l'auto risulta già al limite di carica:
    None se non si sa, perché non è stata letta da quando è stata collegata.
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
    if car_full:
        return Decision(Action.HOLD, "auto già al limite di carica", state)
    if not grid_ok and share_amps(plant, settings) < settings.min_amps:
        if car_full is None:
            # Prima di chiedere il consenso serve sapere se l'auto ha bisogno di caricare
            return None
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
    car_full: bool | None = False,
) -> Decision:
    early = precheck(now, mode, plant, boosting, plugged, grid_ok, state, settings, car_full)
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


# --- carica partita dall'auto senza Octopus ---

# Consumo di casa oltre il quale vale la pena controllare se è l'auto che carica
STRAY_LOAD_W = 1500


def home_load_w(plant: PlantSnapshot) -> float:
    """Consumo della casa: pannelli più prelievo dalla rete più scarica della batteria."""
    return plant.pv_w - plant.grid_w - plant.battery_w


def stray_charge(car: CarStatus, boosting: bool, in_window: bool, target: int | None) -> bool:
    """L'auto carica per conto suo, oltre il livello chiesto a Octopus per stanotte?

    Quando l'auto è già sopra il livello obiettivo Octopus non la tiene più sotto controllo
    e la Tesla carica da sola fino al proprio limite, a prezzo pieno e spesso dalla
    batteria di casa. Una carica immediata o una finestra di Octopus invece sono volute.
    """
    if not car.charging or boosting or in_window or target is None:
        return False
    return car.level >= target


# --- batteria di casa a riposo durante la carica notturna ---

HOLD_MARGIN = timedelta(minutes=2)
HOLD_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class HoldPlan:
    # "hold": blocca la scarica per `seconds`; "release": sblocca subito
    action: str
    seconds: int = 0
    until: datetime | None = None


def plan_battery_hold(
    now: datetime,
    windows: list[tuple[datetime, datetime]],
    held_until: datetime | None,
    settings: Settings,
) -> HoldPlan | None:
    """Decide se tenere a riposo la batteria di casa mentre Octopus carica l'auto di notte.

    Durante una finestra di carica notturna la batteria di casa non deve scaricarsi
    nell'auto: l'auto prende dalla rete a prezzo scontato e la batteria resta per la casa.
    Restituisce None se non c'è nulla da fare.
    """
    end = None
    if not _in_day(now, settings):
        # Fine della catena di finestre contigue che contiene questo momento
        cursor = now
        for start, stop in sorted(windows):
            if start <= cursor + HOLD_TOLERANCE and stop > cursor:
                cursor = end = stop
    if end is None:
        # Nessuna carica in corso: se un blocco è ancora attivo, non serve più
        if held_until is not None and held_until > now:
            return HoldPlan("release")
        return None
    if held_until is not None and held_until >= end - HOLD_TOLERANCE:
        return None
    seconds = int((end - now + HOLD_MARGIN).total_seconds())
    return HoldPlan("hold", seconds=seconds, until=end)
