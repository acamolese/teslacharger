"""Ciclo di controllo: legge Solax, Octopus e l'auto, decide ed esegue."""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .emmeti import EmmetiClient, describe
from .history import History
from .octopus import OctopusClient
from .policy import Action, CarStatus, ControlState, Decision, Mode, decide, plan_battery_hold, precheck
from .push import PushService
from .solax import MAX_HOLD_SECONDS, SolaxClient
from . import weather
from .tesla import TeslaCar, charging_history

log = logging.getLogger("teslacharger")
MAX_EVENTS = 40
DISPATCH_SYNC = timedelta(minutes=30)
ERRORS_BEFORE_ALERT = 3
# Dopo il risveglio l'auto risponde in genere entro mezzo minuto
# Per quanto tempo il livello letto dall'auto resta attendibile, se non viene scollegata
CAR_READING_VALID = timedelta(hours=12)
HOME_CACHE = timedelta(minutes=5)
BILLS_CACHE = timedelta(hours=1)
FORECAST_CACHE = timedelta(hours=1)
CLIMATE_CACHE = timedelta(minutes=2)
CLIMATE_MONTHS = 12
BILLED_MONTHS_SHOWN = 6
WAKE_ATTEMPTS = 8
WAKE_PAUSE_SECONDS = 6
MODE_NAMES = {Mode.BOOST: "Carica subito", Mode.SOLAR: "Carica col sole", Mode.AUTO: "Automatica"}
ACTION_ICONS = {
    Action.START: "play_circle",
    Action.STOP: "stop_circle",
    Action.SET_AMPS: "tune",
    Action.WAKE: "power_settings_new",
}


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
        # Batteria di casa a riposo durante la carica notturna: scelta dell'utente e blocco in corso
        # Domanda della sera ("carico stanotte o domani sono a casa?") e livello pieno preferito
        self.evening: dict | None = None
        self.full_target = 100
        self.hold_enabled = True
        self.held_until: datetime | None = None
        self.state = ControlState()
        self.events: list[dict] = []
        self.status: dict = {}
        self._was_plugged: bool | None = None
        # True se l'auto è stata scollegata dopo l'ultima lettura: il livello noto non vale più
        self._car_stale = False
        self._last_dispatch_sync: datetime | None = None
        self._errors = 0
        self._hold_error: str | None = None
        self._home: dict | None = None
        self._home_time: datetime | None = None
        self.emmeti = EmmetiClient()
        self._climate: dict | None = None
        self._climate_time: datetime | None = None
        self._climate_months: dict[str, dict] = {}
        self._climate_loader: threading.Thread | None = None
        self._forecast: dict | None = None
        self._forecast_time: datetime | None = None
        self._bills: dict | None = None
        self._bills_time: datetime | None = None
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
        self.evening = saved.get("evening")
        self.full_target = saved.get("full_target", 100)
        self.hold_enabled = saved.get("hold_enabled", True)
        if saved.get("held_until"):
            self.held_until = datetime.fromisoformat(saved["held_until"])
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
                "evening": self.evening,
                "full_target": self.full_target,
                "hold_enabled": self.hold_enabled,
                "held_until": self.held_until.isoformat() if self.held_until else None,
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
            self._event("swap_horiz", f"Modalità {MODE_NAMES[mode]}", "Scelta da te")
            self._save()
        self._wakeup.set()

    def set_grid_ok(self, allow: bool) -> None:
        """Consenso a caricare da rete o batteria di casa, valido fino a fine giornata."""
        with self._lock:
            now = datetime.now()
            self.grid_ok_until = datetime.combine(now.date(), self.settings.day_end) if allow else None
            if allow:
                end = self.settings.day_end.strftime("%H:%M")
                self._event("verified_user", f"Consenso dato fino alle {end}", "Rete e batteria di casa")
            else:
                self._event("gpp_bad", "Consenso revocato", "Scelto da te")
            self._save()
        self._wakeup.set()

    def set_hold(self, enabled: bool) -> None:
        """Attiva o disattiva il riposo della batteria di casa durante la carica notturna."""
        with self._lock:
            self.hold_enabled = enabled
            self._event(
                "battery_saver",
                "Batteria di casa a riposo di notte: " + ("attivata" if enabled else "disattivata"),
                "Scelta da te",
            )
            self._save()
        self._wakeup.set()

    def set_target(self, percent: int | None = None, ready_time: str | None = None) -> None:
        """Livello di carica che Octopus deve raggiungere di notte, e ora entro cui farlo."""
        with self._lock:
            vehicle = self.octopus.vehicle()
            percent = percent if percent is not None else vehicle.target_percent or 100
            ready_time = ready_time or vehicle.target_time or "09:00"
            title = f"Carica notturna: {percent}% entro le {ready_time}"
            # Un livello scelto a mano diventa quello da ripristinare nelle notti di carica piena
            self.full_target = percent
            if self.settings.live:
                self.octopus.set_target(vehicle.device_id, percent, ready_time)
                self._event("bedtime", title, "Modificata da te")
            else:
                self._event("bedtime", title, "Modalità di prova: non inviata a Octopus")
            self._save()
        self._wakeup.set()

    def refresh_car(self) -> bool:
        """Lettura dell'auto richiesta dall'utente: se dorme la sveglia e aspetta che risponda."""
        car = self.car.status()
        if car is None:
            self.car.wake()
            for _ in range(WAKE_ATTEMPTS):
                time.sleep(WAKE_PAUSE_SECONDS)
                car = self.car.status()
                if car is not None:
                    break
        with self._lock:
            if car is not None:
                self._store_car(datetime.now(), car)
                self._save()
            return car is not None

    # --- domanda della sera ---

    def _evening_question(self, now: datetime, vehicle, level: int | None) -> None:
        """Dopo le 18, con l'auto collegata, chiede se caricare stanotte o aspettare il sole di domani."""
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        asking = self.evening and self.evening.get("day") == tomorrow
        if not asking:
            if not vehicle.plugged or now.hour < self.settings.evening_ask_hour:
                return
            day = next((d for d in self.forecast_summary().get("days", []) if d["day"] == tomorrow), None)
            home_kwh = day["home_kwh"] if day else 0.0
            suggestion = "home" if home_kwh >= self.settings.home_day_min_kwh else "night"
            self.evening = {
                "day": tomorrow,
                "asked_at": now.isoformat(timespec="seconds"),
                "suggestion": suggestion,
                "home_day": bool(day and day["home_day"]),
                "home_kwh": home_kwh,
                "weather": day["text"] if day else None,
                "level": level,
                "choice": None,
            }
            self.history.log_evening(self.evening)
            self._event("help", "Domanda della sera", "Carico stanotte o domani l'auto resta a casa?")
            if suggestion == "home":
                body = f"Domani di solito sei a casa: previsti fino a {home_kwh:.1f} kWh dal sole. Tocca per scegliere.".replace(".", ",", 1)
            else:
                body = "Carico stanotte con Octopus? Tocca per scegliere, altrimenti carico alle 22."
            self._notify("Auto collegata", body)
            return
        if self.evening.get("choice") is None and now.hour >= self.settings.evening_default_hour:
            # Nessuna risposta: si applica il suggerimento
            self._apply_evening(self.evening["suggestion"], by_user=False)

    def _apply_evening(self, choice: str, by_user: bool) -> None:
        home = choice == "home"
        percent = min(self.settings.home_day_target, self.full_target) if home else self.full_target
        vehicle = self.octopus.vehicle()
        if self.settings.live:
            self.octopus.set_target(vehicle.device_id, percent, vehicle.target_time or "09:00")
        self.evening.update(choice=choice, by_user=by_user, percent=percent)
        self.history.log_evening(self.evening)
        who = "Scelto da te" if by_user else "Nessuna risposta: ho seguito il suggerimento"
        if home:
            self._event("wb_sunny", f"Domani a casa: stanotte carico solo fino al {percent}%", who)
        else:
            self._event("bedtime", f"Stanotte carico fino al {percent}%", who)
        if not by_user:
            self._notify(
                "Ho deciso io per stanotte",
                f"Carico fino al {percent}%" + (" e domani uso il sole." if home else " con Octopus.") + " Puoi cambiare dall'app.",
            )

    def answer_evening(self, choice: str) -> None:
        if choice not in ("home", "night"):
            raise ValueError("scelta non valida")
        with self._lock:
            if not self.evening:
                raise ValueError("nessuna domanda in corso")
            self._apply_evening(choice, by_user=True)
            self._save()
        self._wakeup.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                **self.status,
                "mode": self.mode.value,
                "live": self.settings.live,
                "poll_seconds": self.settings.poll_seconds,
                "grid_ok": self._grid_ok(datetime.now()),
                "evening": self.evening
                if self.evening and self.evening.get("day", "") >= datetime.now().date().isoformat() else None,
                "home_day_target": self.settings.home_day_target,
                "full_target": self.full_target,
                "hold": {
                    "enabled": self.hold_enabled,
                    "until": self.held_until.strftime("%H:%M")
                    if self.held_until and self.held_until > datetime.now() else None,
                },
                "day_start": self.settings.day_start.strftime("%H:%M"),
                "day_end": self.settings.day_end.strftime("%H:%M"),
                "min_amps": self.settings.min_amps,
                "max_amps": self.settings.max_amps,
                "pv_share": self.settings.pv_share,
                "events": list(reversed(self.events)),
            }

    def home_summary(self) -> dict:
        """Dati dell'impianto per il pannello della casa, riletti al massimo ogni 5 minuti."""
        now = datetime.now()
        if self._home is None or now - self._home_time > HOME_CACHE:
            self._home = self.solax.home_summary()
            self._home_time = now
        return self._home

    def forecast_summary(self) -> dict:
        """Meteo e produzione prevista per oggi e i due giorni seguenti, riletti ogni ora."""
        now = datetime.now()
        if self._forecast is None or now - self._forecast_time > FORECAST_CACHE:
            data = weather.fetch(*self.solax.coordinates())
            produced = {d["day"]: d["pv"] for d in self.home_summary()["days"]}
            factor = weather.yield_factor(data, produced)
            self._forecast = {
                "calibrated": factor is not None,
                "days": weather.forecast(data, factor, self.settings, now) if factor else [],
                "read": now.isoformat(timespec="minutes"),
            }
            self._forecast_time = now
        return self._forecast

    def climate_summary(self) -> dict:
        """Pompa di calore: stato, stanze, acqua calda e consumi, riletti al massimo ogni due minuti."""
        if not self.emmeti.configured:
            return {"configured": False}
        now = datetime.now()
        if self._climate is None or now - self._climate_time > CLIMATE_CACHE:
            today = now.date()
            names = dict(
                pair.split(":", 1) for pair in os.environ.get("ROOM_NAMES", "").split(",") if ":" in pair
            )
            rooms = self.emmeti.rooms()
            for room in rooms:
                # I nomi si associano all'indirizzo del termostato, che è stabile
                room["label"] = names.get(str(room["address"]), f"Stanza {room['name']}")
            self._climate = {
                "configured": True,
                "time": now.isoformat(timespec="seconds"),
                **describe(self.emmeti.registers()),
                "rooms": rooms,
                "power": self.emmeti.power(),
                "today": self.emmeti.energy(today, today + timedelta(days=1)),
            }
            self._climate_time = now
        if self._climate_loader is None or not self._climate_loader.is_alive():
            self._climate_loader = threading.Thread(target=self._load_climate_months, daemon=True)
            self._climate_loader.start()
        months = [self._climate_months[m] for m in sorted(self._climate_months)][-CLIMATE_MONTHS:]
        return {**self._climate, "months": months}

    def _load_climate_months(self) -> None:
        """Consumi mensili dell'ultimo anno. I mesi conclusi si leggono una volta sola e si conservano."""
        cache_file = data_dir() / "emmeti-months.json"
        if not self._climate_months and cache_file.exists():
            self._climate_months = json.loads(cache_file.read_text())
        this_month = date.today().replace(day=1)
        cursor = this_month
        for _ in range(CLIMATE_MONTHS):
            key = cursor.strftime("%Y-%m")
            current = cursor == this_month
            known = self._climate_months.get(key)
            stale = current and (not known or known.get("read_on") != date.today().isoformat())
            if known is None or stale:
                try:
                    energy = self.emmeti.month_energy(cursor.year, cursor.month)
                except Exception as err:
                    log.warning("consumi Emmeti di %s non letti: %s", key, err)
                    energy = None
                if energy:
                    self._climate_months[key] = {"month": key, **energy, "read_on": date.today().isoformat()}
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(self._climate_months))

    def bills_summary(self) -> dict:
        """Bollette emesse e stima dei mesi non ancora fatturati, rilette al massimo ogni ora."""
        now = datetime.now()
        if self._bills is None or now - self._bills_time > BILLS_CACHE:
            self._bills = self._build_bills(now)
            self._bills_time = now
        return self._bills

    def _build_bills(self, now: datetime) -> dict:
        billing = self.octopus.billing()
        # Ogni addebito porta la data dell'ultimo giorno del mese a cui si riferisce
        billed = {c["date"][:7]: c for c in billing["charges"]}
        payments = sorted(billing["payments"], key=lambda p: p["date"])
        this_month = now.strftime("%Y-%m")
        first_unbilled = max(billed) if billed else this_month
        months = sorted(billed)[-BILLED_MONTHS_SHOWN:]
        cursor = date(int(first_unbilled[:4]), int(first_unbilled[5:]), 1)
        while cursor.strftime("%Y-%m") < this_month:
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            months.append(cursor.strftime("%Y-%m"))
        rows = []
        for month in dict.fromkeys(months):
            energy = self.solax.month_totals(month)
            row = {"month": month, "kwh": energy["imported"], "estimated": month not in billed}
            if month in billed:
                charge = billed[month]
                paid = next(
                    (p["date"] for p in payments if p["date"] >= charge["date"] and abs(p["amount"] - charge["amount"]) < 0.01),
                    None,
                )
                row.update(amount=charge["amount"], paid=paid)
            else:
                in_progress = month == this_month
                days_in_month = ((date(int(month[:4]), int(month[5:]), 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
                share = now.day / days_in_month if in_progress else 1
                row.update(
                    amount=round(energy["imported"] * self.settings.price_kwh + self.settings.fixed_monthly * share, 2),
                    in_progress=in_progress,
                )
            rows.append(row)
        return {
            "owed": billing["owed"],
            "rows": list(reversed(rows)),
            "prices": {"kwh": self.settings.price_kwh, "fixed_monthly": self.settings.fixed_monthly},
        }

    def history_summary(self) -> dict:
        window = (self.settings.day_start, self.settings.day_end)
        month = date.today().isoformat()[:7]
        monthly = self.history.daily(31, *window)
        totals = {
            key: round(sum(d[key] for d in monthly if d["day"].startswith(month)), 1)
            for key in ("sun", "day_other", "night")
        }
        return {
            "days": monthly[-14:],
            "month": totals,
            "since": self.history.since(),
            "prices": {"kwh": self.settings.price_kwh, "night_discount": self.settings.night_discount_kwh},
            "consumption": self.history.consumption(),
            "insights": self.history.insights(),
            "supercharges": self.history.supercharges(),
            "supercharge_month": self.history.supercharge_month(month),
            "km_month": self.history.km_since(month + "-01"),
        }

    # --- ciclo ----------------------------------------------------------------

    def _grid_ok(self, now: datetime) -> bool:
        return self.grid_ok_until is not None and now < self.grid_ok_until

    def _event(self, icon: str, title: str, sub: str = "") -> None:
        self.events.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "icon": icon,
            "title": title,
            "sub": sub,
        })
        del self.events[:-MAX_EVENTS]
        # Nello storico restano tutte le manovre, non solo le ultime mostrate nell'app
        self.history.log_event(self.events[-1]["time"], icon, title, sub)

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
                    self._event("error", "Ciclo non riuscito", str(err))
                    self._notify(
                        "TeslaCharger ha un problema",
                        f"Tre cicli di fila non sono riusciti: {err}. Tocca per i dettagli.",
                    )
            self.status["time"] = now.isoformat(timespec="seconds")
            self._save()

    def _read_car(self, now: datetime) -> CarStatus | None:
        car = self.car.status()
        self._store_car(now, car)
        return car

    def _car_full(self, now: datetime) -> bool | None:
        """L'auto è già al limite di carica? None se l'ultima lettura non è più attendibile."""
        info = self.status.get("car_info")
        if not info or self._car_stale or info.get("level") is None:
            return None
        if now - datetime.fromisoformat(info["time"]) > CAR_READING_VALID:
            return None
        return info["level"] >= (info.get("limit") or 100)

    def _store_car(self, now: datetime, car: CarStatus | None) -> None:
        if car is not None:
            self._car_stale = False
        if car is not None and self.car.last_info:
            info = {**self.car.last_info, "time": now.isoformat(timespec="seconds")}
            self.status["car_info"] = info
            self.history.log_car(now, info)
        self.status["car"] = {**asdict(car), "time": now.isoformat(timespec="seconds")} if car else None

    def _auto_switch(self, now: datetime, ours: bool) -> None:
        """Passaggi automatici: al mattino a "carica col sole", la sera ad "automatica"."""
        today = now.date().isoformat()
        in_day = self.settings.day_start <= now.time() < self.settings.day_end
        if in_day and self.switched_on != today:
            self.switched_on = today
            if self.mode is Mode.AUTO:
                self.mode = Mode.SOLAR
                self._event("swap_horiz", "Modalità Carica col sole", "Passaggio automatico del mattino")
        elif not in_day and self.mode is Mode.SOLAR and not ours:
            self.mode = Mode.AUTO
            self._event("swap_horiz", "Modalità Automatica", "Passaggio automatico della sera")

    def _sync_dispatches(self, now: datetime) -> None:
        if self._last_dispatch_sync and now - self._last_dispatch_sync < DISPATCH_SYNC:
            return
        self._last_dispatch_sync = now
        for row in self.octopus.completed_dispatches():
            start = datetime.fromisoformat(row["start"]).astimezone().replace(tzinfo=None)
            end = datetime.fromisoformat(row["end"]).astimezone().replace(tzinfo=None)
            self.history.log_dispatch(start, end, row["kwh"])
        if self.settings.live or os.path.exists(os.environ.get("TESLA_TOKEN_FILE", "tesla-tokens.json")):
            try:
                for session in charging_history():
                    self.history.log_supercharge(session)
            except Exception as err:
                log.warning("storico delle ricariche Tesla non letto: %s", err)
        for session in self.octopus.charging_sessions():
            if not session["end"]:
                continue
            for key in ("start", "end"):
                session[key] = (
                    datetime.fromisoformat(session[key]).astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
                )
            self.history.log_session(session)

    def _hold_battery(self, now: datetime, windows: list) -> None:
        """Tiene a riposo la batteria di casa mentre Octopus carica l'auto di notte."""
        if not self.settings.live:
            return
        # Con l'opzione spenta resta solo da sbloccare un eventuale blocco in corso
        plan = plan_battery_hold(now, windows if self.hold_enabled else [], self.held_until, self.settings)
        if plan is None:
            return
        if plan.action == "release":
            self.solax.release_battery()
            self.held_until = None
            self._event("battery_saver", "Batteria di casa di nuovo disponibile", "La carica notturna non è più in corso")
            return
        self.solax.hold_battery(min(plan.seconds, MAX_HOLD_SECONDS))
        self.held_until = plan.until
        self._event(
            "battery_saver",
            f"Batteria di casa a riposo fino alle {plan.until:%H:%M}",
            "L'auto carica dalla rete a prezzo scontato",
        )

    def _planned_window(self, device_id: str, now: datetime) -> dict | None:
        """Inizio e fine della prossima carica pianificata da Octopus, in ora locale."""
        windows = []
        for row in self.octopus.planned_dispatches(device_id):
            if row.get("type") == "BOOST":
                continue
            start = datetime.fromisoformat(row["start"]).astimezone().replace(tzinfo=None)
            end = datetime.fromisoformat(row["end"]).astimezone().replace(tzinfo=None)
            if end > now:
                windows.append((start, end))
        try:
            self._hold_battery(now, windows)
        except Exception as err:
            # Un problema con l'inverter non deve fermare la gestione della ricarica
            log.warning("blocco della batteria di casa non riuscito: %s", err)
            if self._hold_error != str(err):
                self._hold_error = str(err)
                self._event("error", "Batteria di casa: comando non riuscito", str(err))
        if not windows:
            return None
        return {
            "start": min(w[0] for w in windows).strftime("%H:%M"),
            "end": max(w[1] for w in windows).strftime("%H:%M"),
        }

    def _cycle(self, now: datetime) -> None:
        plant = self.solax.snapshot()
        vehicle = self.octopus.vehicle()
        self.history.log_plant(
            plant, vehicle.state, self.mode.value, bool(self.held_until and self.held_until > now)
        )
        self.status.update(
            plant={**asdict(plant), "excess_w": plant.excess_w},
            octopus=vehicle.state,
            target={"percent": vehicle.target_percent, "time": vehicle.target_time},
            push=self.push.enabled,
        )
        self._sync_dispatches(now)
        self.status["planned"] = self._planned_window(vehicle.device_id, now)
        self._auto_switch(now, vehicle.boosting and self.state.started_by_us)

        if not vehicle.plugged:
            self._car_stale = True
        hints = (vehicle.boosting, vehicle.plugged, self._grid_ok(now))
        car_full = self._car_full(now)
        need_car = precheck(now, self.mode, plant, *hints, self.state, self.settings, car_full) is None
        # Appena l'auto viene collegata è sveglia: una lettura costa poco e aggiorna il pannello
        just_plugged = vehicle.plugged and self._was_plugged is False
        just_unplugged = not vehicle.plugged and self._was_plugged is True
        self._was_plugged = vehicle.plugged
        car = self._read_car(now) if need_car or just_plugged else None
        if just_plugged or just_unplugged:
            self.history.log_plug(now, vehicle.plugged, car.level if car else None)
        try:
            known = self.status.get("car_info") or {}
            self._evening_question(now, vehicle, car.level if car else known.get("level"))
        except Exception as err:
            log.warning("domanda della sera non riuscita: %s", err)

        decision = decide(
            now, self.mode, plant, car if need_car else None, *hints, self.state, self.settings,
            # Dopo una lettura appena fatta decide il dato fresco dell'auto, non quello ricordato
            None if need_car else car_full,
        )
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
            end = self.settings.day_end.strftime("%H:%M")
            self._event("notifications", "Richiesta di consenso", f"Pannelli a {plant.pv_w:.0f} W, sotto il minimo")
            self._notify(
                "Sole insufficiente per l'auto",
                f"Pannelli a {kw} kW. Tocca per caricare al minimo da batteria di casa e rete fino alle {end}.",
            )
        if decision.action is Action.HOLD:
            self.state = decision.state
            return

        reason = decision.reason[:1].upper() + decision.reason[1:]
        title = {
            Action.START: f"Carica avviata a {decision.amps} A",
            Action.STOP: "Carica fermata",
            Action.SET_AMPS: f"Potenza regolata a {decision.amps} A",
            Action.WAKE: "Risveglio dell'auto",
        }[decision.action]
        if not self.settings.live:
            self._event("science", f"Prova: {title[:1].lower() + title[1:]}", reason)
            # Il comando non è partito: si ricordano solo le attese, non la manovra
            self.state = replace(
                self.state,
                last_data_time=decision.state.last_data_time,
                last_wake=decision.state.last_wake,
                idle_at=decision.state.idle_at,
            )
            return

        self._execute(decision, vehicle.device_id)
        self._event(ACTION_ICONS[decision.action], title, reason)
        self.state = decision.state
        if decision.action is Action.START:
            self._notify("Carica avviata", f"{decision.amps} A all'auto. {reason}")
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
                self._event("error", "Corrente massima non ripristinata", str(err))

    def run_forever(self) -> None:
        while True:
            self.cycle()
            self._wakeup.wait(self.settings.poll_seconds)
            self._wakeup.clear()
