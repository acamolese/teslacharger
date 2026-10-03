"""Regole di decisione: quando avviare e quando fermare la carica solare.

Funzioni pure, senza chiamate di rete, così si possono provare con dati finti.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .config import Settings
from .solax import PlantSnapshot


class Action(Enum):
    START = "avvia"
    STOP = "ferma"
    HOLD = "nessuna azione"


@dataclass(frozen=True)
class ControlState:
    # True se la carica in corso è stata avviata da questo sistema
    started_by_us: bool = False
    last_switch: datetime | None = None
    deficit_count: int = 0
    # Ultimo dato Solax già valutato, per non contare due volte la stessa lettura
    last_data_time: str | None = None


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    state: ControlState


def decide(
    now: datetime,
    plant: PlantSnapshot,
    boosting: bool,
    state: ControlState,
    settings: Settings,
) -> Decision:
    fresh = plant.data_time != state.last_data_time
    state = _replace(state, last_data_time=plant.data_time)
    in_day = settings.day_start <= now.time() < settings.day_end
    can_switch = state.last_switch is None or now - state.last_switch >= timedelta(
        minutes=settings.min_switch_minutes
    )

    if boosting and not state.started_by_us:
        # Carica avviata a mano dall'app di Octopus: non è nostra, non la tocchiamo
        return Decision(Action.HOLD, "carica immediata avviata manualmente", state)

    if not boosting:
        state = _replace(state, started_by_us=False, deficit_count=0)
        if not in_day:
            return Decision(Action.HOLD, "fuori dalla fascia diurna", state)
        if plant.battery_soc < settings.soc_start:
            return Decision(
                Action.HOLD,
                f"batteria di casa al {plant.battery_soc}%, sotto la soglia di avvio {settings.soc_start}%",
                state,
            )
        needed = settings.car_power_w - settings.battery_assist_w
        if plant.excess_w < needed:
            return Decision(
                Action.HOLD,
                f"surplus {plant.excess_w:.0f} W, ne servono {needed}",
                state,
            )
        if not can_switch:
            return Decision(Action.HOLD, "surplus sufficiente, attesa tra due manovre", state)
        return Decision(
            Action.START,
            f"surplus {plant.excess_w:.0f} W, batteria di casa al {plant.battery_soc}%",
            _replace(state, started_by_us=True, last_switch=now),
        )

    # Carica in corso avviata da noi: nel surplus è già compreso il consumo dell'auto
    stop_reason = None
    if not in_day:
        stop_reason = "fine della fascia diurna"
    elif plant.battery_soc < settings.soc_stop:
        stop_reason = f"batteria di casa al {plant.battery_soc}%, sotto la soglia di stop {settings.soc_stop}%"
    else:
        if fresh:
            in_deficit = plant.excess_w < -settings.battery_assist_w
            state = _replace(state, deficit_count=state.deficit_count + 1 if in_deficit else 0)
        if state.deficit_count >= settings.deficit_samples:
            stop_reason = f"deficit per {state.deficit_count} letture consecutive ({plant.excess_w:.0f} W)"

    if stop_reason is None:
        return Decision(Action.HOLD, f"carica solare in corso, surplus {plant.excess_w:.0f} W", state)
    if not can_switch and in_day:
        return Decision(Action.HOLD, f"{stop_reason}, attesa tra due manovre", state)
    return Decision(
        Action.STOP,
        stop_reason,
        _replace(state, started_by_us=False, last_switch=now, deficit_count=0),
    )


def _replace(state: ControlState, **changes) -> ControlState:
    return ControlState(**{**state.__dict__, **changes})
