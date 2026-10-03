"""Ciclo di controllo: legge Solax, decide, comanda la carica immediata di Octopus.

Uso:
  python3 -m teslacharger.controller --once        una sola valutazione, senza agire
  python3 -m teslacharger.controller               ciclo continuo, senza agire
  python3 -m teslacharger.controller --live        ciclo continuo che comanda davvero l'auto
"""

import argparse
import logging
import time
from datetime import datetime

from .config import Settings, load_env
from .octopus import OctopusClient
from .policy import Action, ControlState, decide
from .solax import SolaxClient

log = logging.getLogger("teslacharger")


def step(solax, octopus, state: ControlState, settings: Settings, live: bool) -> ControlState:
    plant = solax.snapshot()
    vehicle = octopus.vehicle()
    decision = decide(datetime.now(), plant, vehicle.boosting, state, settings)
    log.info(
        "pannelli %.0f W, batteria casa %d%% (%+.0f W), rete %+.0f W, auto %s | %s: %s",
        plant.pv_w,
        plant.battery_soc,
        plant.battery_w,
        plant.grid_w,
        vehicle.state,
        decision.action.value,
        decision.reason,
    )
    if decision.action is Action.HOLD:
        return decision.state
    if not live:
        log.info("modalità di prova: comando non inviato")
        # Senza comando inviato lo stato non deve registrare la manovra
        return ControlState(last_data_time=decision.state.last_data_time)
    if decision.action is Action.START:
        octopus.start_boost(vehicle.device_id)
    else:
        octopus.cancel_boost(vehicle.device_id)
    return decision.state


def main() -> None:
    parser = argparse.ArgumentParser(description="Carica solare della Tesla tramite Octopus")
    parser.add_argument("--once", action="store_true", help="una sola valutazione")
    parser.add_argument("--live", action="store_true", help="invia davvero i comandi all'auto")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_env()
    settings = Settings.from_env()
    solax, octopus = SolaxClient(), OctopusClient()
    state = ControlState()

    while True:
        try:
            state = step(solax, octopus, state, settings, args.live)
        except Exception as err:  # un errore di rete non deve fermare il ciclo
            log.error("ciclo non riuscito: %s", err)
            if args.once:
                raise
        if args.once:
            return
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
