"""Ciclo di controllo: legge Solax, Octopus e l'auto, decide ed esegue."""

import json
import logging
import os
import threading
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .config import Settings
from .octopus import OctopusClient
from .policy import Action, CarStatus, ControlState, Decision, Mode, decide, precheck
from .solax import SolaxClient
from .tesla import TeslaCar

log = logging.getLogger("teslacharger")
MAX_EVENTS = 40


class Controller:
    def __init__(self, settings: Settings, solax=None, octopus=None, car=None):
        self.settings = settings
        self.solax = solax or SolaxClient()
        self.octopus = octopus or OctopusClient()
        self.car = car or TeslaCar()
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._file = Path(os.environ.get("TESLACHARGER_DATA", "data")) / "state.json"
        self.mode = Mode.AUTO
        # Modalità a cui tornare quando "carica subito" ha finito
        self.after_boost = Mode.AUTO
        self._vehicle = None
        # Fino a quando l'utente consente di caricare da rete o batteria senza sole
        self.grid_ok_until: datetime | None = None
        self.state = ControlState()
        self.events: list[dict] = []
        self.status: dict = {}
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        saved = json.loads(self._file.read_text())
        try:
            self.mode = Mode(saved.get("mode", Mode.AUTO.value))
            self.after_boost = Mode(saved.get("after_boost", Mode.AUTO.value))
        except ValueError:
            # Modalità di una versione precedente: si riparte da quella che non interviene
            self.mode = self.after_boost = Mode.AUTO
        self.state = ControlState.from_json(saved.get("state", {}))
        self.events = saved.get("events", [])
        if saved.get("grid_ok_until"):
            self.grid_ok_until = datetime.fromisoformat(saved["grid_ok_until"])

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps({
                "mode": self.mode.value,
                "after_boost": self.after_boost.value,
                "state": self.state.to_json(),
                "events": self.events,
                "grid_ok_until": self.grid_ok_until.isoformat() if self.grid_ok_until else None,
            })
        )

    def set_mode(self, mode: Mode) -> None:
        with self._lock:
            if mode is Mode.BOOST and self.mode is not Mode.BOOST:
                self.after_boost = self.mode
            self.mode = mode
            # Una scelta esplicita dell'utente azzera le attese su risveglio e cavo
            self.state = replace(self.state, last_wake=None, idle_at=None)
            self._event(f"modalità impostata: {mode.value}")
            self._save()
        self._wakeup.set()

    def set_grid_ok(self, allow: bool) -> None:
        """Consenso a caricare da rete o batteria di casa, valido fino a fine giornata."""
        with self._lock:
            now = datetime.now()
            self.grid_ok_until = datetime.combine(now.date(), self.settings.day_end) if allow else None
            self._event("rete e batteria autorizzate per oggi" if allow else "autorizzazione a rete e batteria revocata")
            self._save()
        self._wakeup.set()

    def set_target(self, percent: int | None = None, ready_time: str | None = None) -> None:
        """Livello di carica che Octopus deve raggiungere di notte, e ora entro cui farlo."""
        with self._lock:
            vehicle = self.octopus.vehicle()
            percent = percent if percent is not None else vehicle.target_percent or 100
            ready_time = ready_time or vehicle.target_time or "09:00"
            text = f"carica notturna: {percent}% entro le {ready_time}"
            if self.settings.live:
                self.octopus.set_target(vehicle.device_id, percent, ready_time)
                self._event(text)
            else:
                self._event(f"[prova] {text}")
            self._save()
        self._wakeup.set()

    def _grid_ok(self, now: datetime) -> bool:
        return self.grid_ok_until is not None and now < self.grid_ok_until

    def snapshot(self) -> dict:
        with self._lock:
            return {
                **self.status,
                "mode": self.mode.value,
                "live": self.settings.live,
                "grid_ok": self._grid_ok(datetime.now()),
                "day_end": self.settings.day_end.strftime("%H:%M"),
                "events": list(reversed(self.events)),
            }

    def _event(self, text: str) -> None:
        self.events.append({"time": datetime.now().isoformat(timespec="seconds"), "text": text})
        del self.events[:-MAX_EVENTS]

    def cycle(self) -> None:
        with self._lock:
            now = datetime.now()
            try:
                self._cycle(now)
                self.status["error"] = None
            except Exception as err:
                log.exception("ciclo non riuscito")
                self.status["error"] = str(err)
            self.status["time"] = now.isoformat(timespec="seconds")
            self._save()

    def _cycle(self, now: datetime) -> None:
        plant = self.solax.snapshot()
        vehicle = self._vehicle = self.octopus.vehicle()
        self.status.update(
            plant={**asdict(plant), "excess_w": plant.excess_w},
            octopus=vehicle.state,
            target={"percent": vehicle.target_percent, "time": vehicle.target_time},
        )

        car: CarStatus | None = None
        hints = (vehicle.boosting, vehicle.plugged, self._grid_ok(now))
        if precheck(now, self.mode, plant, *hints, self.state, self.settings) is None:
            car = self.car.status()
            self.status["car"] = {**asdict(car), "time": now.isoformat(timespec="seconds")} if car else None
        decision = decide(now, self.mode, plant, car, *hints, self.state, self.settings)
        self.status["decision"] = {
            "action": decision.action.value,
            "reason": decision.reason,
            "amps": decision.amps,
            "ask": decision.ask,
        }
        log.info(
            "pannelli %.0f W, surplus %+.0f W, batteria casa %d%%, auto %s | %s: %s",
            plant.pv_w, plant.excess_w, plant.battery_soc, vehicle.state,
            decision.action.value, decision.reason,
        )
        if decision.action is Action.HOLD:
            self.state = decision.state
            return

        amps = f" a {decision.amps} A" if decision.amps else ""
        if not self.settings.live:
            self._event(f"[prova] {decision.action.value}{amps}: {decision.reason}")
            # Il comando non è partito: si ricordano solo le attese, non la manovra
            self.state = replace(
                self.state,
                last_data_time=decision.state.last_data_time,
                last_wake=decision.state.last_wake,
                idle_at=decision.state.idle_at,
            )
            return

        self._execute(decision, vehicle.device_id)
        self._event(f"{decision.action.value}{amps}: {decision.reason}")
        self.state = decision.state
        if decision.action is Action.STOP and self.mode is Mode.BOOST:
            self.mode = self.after_boost

    def _execute(self, decision: Decision, device_id: str) -> None:
        if decision.action is Action.WAKE:
            self.car.wake()
        elif decision.action is Action.SET_AMPS:
            self.car.set_amps(decision.amps)
        elif decision.action is Action.START:
            self.car.set_amps(decision.amps)
            self.octopus.start_boost(device_id)
        elif decision.action is Action.STOP:
            self.octopus.cancel_boost(device_id)
            try:
                # Ripristina la corrente massima, altrimenti la carica notturna resterebbe lenta
                self.car.set_amps(decision.amps)
            except Exception as err:
                log.warning("corrente massima non ripristinata: %s", err)
                self._event(f"attenzione: corrente massima non ripristinata ({err})")

    def run_forever(self) -> None:
        while True:
            self.cycle()
            self._wakeup.wait(self.settings.poll_seconds)
            self._wakeup.clear()
