"""Ciclo di controllo: legge Solax, Octopus e l'auto, decide ed esegue."""

import json
import logging
import os
import threading
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .history import History
from .octopus import OctopusClient
from .policy import Action, CarStatus, ControlState, Decision, Mode, decide, precheck
from .push import PushService
from .solax import SolaxClient
from .tesla import TeslaCar

log = logging.getLogger("teslacharger")
MAX_EVENTS = 40
DISPATCH_SYNC = timedelta(minutes=30)
ERRORS_BEFORE_ALERT = 3


def data_dir() -> Path:
    return Path(os.environ.get("TESLACHARGER_DATA", "data"))


class Controller:
    def __init__(self, settings: Settings, solax=None, octopus=None, car=None, history=None, push=None):
        self.settings = settings
        self.solax = solax or SolaxClient()
        self.octopus = octopus or OctopusClient()
        self.car = car or TeslaCar()
        self.history = history or History(data_dir() / "history.db")
        self.push = push or PushService(
            data_dir(), f"https://{os.environ.get('TESLA_DOMAIN', 'tesla.kilowattzero.it')}"
        )
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._file = data_dir() / "state.json"
        self.mode = Mode.AUTO
        # Modalità a cui tornare quando "carica subito" ha finito
        self.after_boost = Mode.AUTO
        # Fino a quando l'utente consente di caricare da rete o batteria senza sole
        self.grid_ok_until: datetime | None = None
        # Giorni in cui sono già avvenuti il passaggio del mattino e la richiesta di consenso
        self.switched_on: str | None = None
        self.asked_on: str | None = None
        self.state = ControlState()
        self.events: list[dict] = []
        self.status: dict = {}
        self._was_plugged: bool | None = None
        self._last_dispatch_sync: datetime | None = None
        self._errors = 0
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
        self.switched_on = saved.get("switched_on")
        self.asked_on = saved.get("asked_on")
        self.status["car_info"] = saved.get("car_info")
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
                "switched_on": self.switched_on,
                "asked_on": self.asked_on,
                "car_info": self.status.get("car_info"),
                "grid_ok_until": self.grid_ok_until.isoformat() if self.grid_ok_until else None,
            })
        )

    # --- comandi dalla webapp -------------------------------------------------

    def set_mode(self, mode: Mode) -> None:
        with self._lock:
            if mode is Mode.BOOST and self.mode is not Mode.BOOST:
                self.after_boost = self.mode
            self.mode = mode
            # Una scelta fatta di giorno vale fino a sera: niente passaggio automatico dopo.
            # Scelta prima del mattino, "automatica" passa comunque a "carica col sole".
            now = datetime.now()
            if self.settings.day_start <= now.time() < self.settings.day_end:
                self.switched_on = now.date().isoformat()
            # e azzera le attese su risveglio e cavo
            self.state = replace(self.state, last_wake=None, idle_at=None)
            self._event(f"modalità scelta: {mode.value}")
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

    def refresh_car(self) -> bool:
        """Lettura dell'auto richiesta dall'utente. Non la sveglia: False se è in standby."""
        with self._lock:
            read = self._read_car(datetime.now()) is not None
            self._save()
            return read

    def snapshot(self) -> dict:
        with self._lock:
            return {
                **self.status,
                "mode": self.mode.value,
                "live": self.settings.live,
                "grid_ok": self._grid_ok(datetime.now()),
                "day_start": self.settings.day_start.strftime("%H:%M"),
                "day_end": self.settings.day_end.strftime("%H:%M"),
                "max_amps": self.settings.max_amps,
                "pv_share": self.settings.pv_share,
                "events": list(reversed(self.events)),
            }

    def history_summary(self) -> dict:
        window = (self.settings.day_start, self.settings.day_end)
        month = date.today().isoformat()[:7]
        monthly = self.history.daily(31, *window)
        totals = {
            key: round(sum(d[key] for d in monthly if d["day"].startswith(month)), 1)
            for key in ("sun", "day_other", "night")
        }
        return {"days": monthly[-14:], "month": totals, "consumption": self.history.consumption()}

    # --- ciclo ----------------------------------------------------------------

    def _grid_ok(self, now: datetime) -> bool:
        return self.grid_ok_until is not None and now < self.grid_ok_until

    def _event(self, text: str) -> None:
        self.events.append({"time": datetime.now().isoformat(timespec="seconds"), "text": text})
        del self.events[:-MAX_EVENTS]

    def _notify(self, title: str, body: str) -> None:
        if self.push.enabled:
            threading.Thread(target=self.push.send, args=(title, body), daemon=True).start()

    def cycle(self) -> None:
        with self._lock:
            now = datetime.now()
            try:
                self._cycle(now)
                self.status["error"] = None
                self._errors = 0
            except Exception as err:
                log.exception("ciclo non riuscito")
                self.status["error"] = str(err)
                self._errors += 1
                if self._errors == ERRORS_BEFORE_ALERT:
                    self._notify("TeslaCharger ha un problema", f"Gli ultimi cicli non sono riusciti: {err}")
            self.status["time"] = now.isoformat(timespec="seconds")
            self._save()

    def _read_car(self, now: datetime) -> CarStatus | None:
        car = self.car.status()
        if car is not None and self.car.last_info:
            info = {**self.car.last_info, "time": now.isoformat(timespec="seconds")}
            self.status["car_info"] = info
            self.history.log_car(now, info)
        self.status["car"] = {**asdict(car), "time": now.isoformat(timespec="seconds")} if car else None
        return car

    def _auto_switch(self, now: datetime, ours: bool) -> None:
        """Passaggi automatici: al mattino a "carica col sole", la sera ad "automatica"."""
        today = now.date().isoformat()
        in_day = self.settings.day_start <= now.time() < self.settings.day_end
        if in_day and self.switched_on != today:
            self.switched_on = today
            if self.mode is Mode.AUTO:
                self.mode = Mode.SOLAR
                self._event("passaggio automatico a carica col sole")
        elif not in_day and self.mode is Mode.SOLAR and not ours:
            self.mode = Mode.AUTO
            self._event("fuori dalla fascia diurna: carica notturna con Octopus")

    def _sync_dispatches(self, now: datetime) -> None:
        if self._last_dispatch_sync and now - self._last_dispatch_sync < DISPATCH_SYNC:
            return
        self._last_dispatch_sync = now
        for row in self.octopus.completed_dispatches():
            start = datetime.fromisoformat(row["start"]).astimezone().replace(tzinfo=None)
            end = datetime.fromisoformat(row["end"]).astimezone().replace(tzinfo=None)
            self.history.log_dispatch(start, end, row["kwh"])

    def _cycle(self, now: datetime) -> None:
        plant = self.solax.snapshot()
        self.history.log_plant(plant)
        vehicle = self.octopus.vehicle()
        self.status.update(
            plant={**asdict(plant), "excess_w": plant.excess_w},
            octopus=vehicle.state,
            target={"percent": vehicle.target_percent, "time": vehicle.target_time},
            push=self.push.enabled,
        )
        self._sync_dispatches(now)
        self._auto_switch(now, vehicle.boosting and self.state.started_by_us)

        hints = (vehicle.boosting, vehicle.plugged, self._grid_ok(now))
        need_car = precheck(now, self.mode, plant, *hints, self.state, self.settings) is None
        # Appena l'auto viene collegata è sveglia: una lettura costa poco e aggiorna il pannello
        just_plugged = vehicle.plugged and self._was_plugged is False
        self._was_plugged = vehicle.plugged
        car = self._read_car(now) if need_car or just_plugged else None

        decision = decide(now, self.mode, plant, car if need_car else None, *hints, self.state, self.settings)
        self.status["decision"] = {
            "action": decision.action.value,
            "reason": decision.reason,
            "amps": decision.amps,
            "ask": decision.ask,
        }
        log.info(
            "pannelli %.0f W, batteria casa %d%%, auto %s, modalità %s | %s: %s",
            plant.pv_w, plant.battery_soc, vehicle.state, self.mode.value,
            decision.action.value, decision.reason,
        )
        if decision.ask and self.asked_on != now.date().isoformat():
            self.asked_on = now.date().isoformat()
            kw = f"{plant.pv_w / 1000:.1f}".replace(".", ",")
            self._notify(
                "Sole insufficiente per l'auto",
                f"I pannelli danno {kw} kW. Apri l'app per autorizzare la carica da rete o batteria.",
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
        reason = decision.reason[:1].upper() + decision.reason[1:]
        if decision.action is Action.START:
            self._notify(f"Carica avviata a {decision.amps} A", reason)
        elif decision.action is Action.STOP:
            self._notify("Carica fermata", reason)
            if self.mode is Mode.BOOST:
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
                # Riporta la corrente al massimo scelto, altrimenti la carica notturna resterebbe lenta
                self.car.set_amps(decision.amps)
            except Exception as err:
                log.warning("corrente massima non ripristinata: %s", err)
                self._event(f"attenzione: corrente massima non ripristinata ({err})")

    def run_forever(self) -> None:
        while True:
            self.cycle()
            self._wakeup.wait(self.settings.poll_seconds)
            self._wakeup.clear()
