"""Ciclo di controllo: legge Solax, Octopus e l'auto, decide ed esegue."""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings, _home_plan
from .emmeti import EmmetiClient, describe
from .history import History
from .octopus import MIN_TARGET, READY_TIMES, STATE_PLANNED, OctopusClient
from .policy import (
    Action, CarStatus, ControlState, Decision, Mode, decide, enough_sun, plan_battery_hold, precheck,
)
from .push import PushService
from .solax import MAX_HOLD_SECONDS, SolaxClient
from . import weather
from .tesla import TeslaCar, charging_history

log = logging.getLogger("teslacharger")
MAX_EVENTS = 40
DISPATCH_SYNC = timedelta(minutes=30)
ERRORS_BEFORE_ALERT = 3
# Attesa massima della conferma di Octopus prima di segnalare che la manovra non è avvenuta
NOTICE_TIMEOUT = timedelta(minutes=10)
# Attesa dopo il collegamento prima di avviare la carica: nei primi istanti Octopus
# prende in carico l'auto e annulla una carica immediata appena richiesta
PLUG_SETTLE = timedelta(minutes=4)
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
PLAN_NAMES = {"home": "Domani a casa", "night": "Automatico"}
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
        # Giorno in cui si è già avvisato che il sole basta per l'auto
        self.sun_notified_on: str | None = None
        # Batteria di casa a riposo durante la carica notturna: scelta dell'utente e blocco in corso
        # Domanda della sera: nessuna carica, domani a casa oppure automatico
        self.evening: dict | None = None
        # Livello e ora di fine carica delle due notti in cui si carica
        self.plans = {
            "home": {"percent": settings.home_day_target, "time": None},
            "night": {"percent": 100, "time": None},
        }
        self.hold_enabled = True
        self.held_until: datetime | None = None
        self.state = ControlState()
        self.events: list[dict] = []
        self.status: dict = {}
        self._was_plugged: bool | None = None
        self._plugged_at: datetime | None = None
        # True se l'auto è stata scollegata dopo l'ultima lettura: il livello noto non vale più
        self._car_stale = False
        self._last_dispatch_sync: datetime | None = None
        self._errors = 0
        self._hold_error: str | None = None
        # Notifica di avvio o di stop in attesa che Octopus confermi la manovra
        self._notice: dict | None = None
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
        self.sun_notified_on = saved.get("sun_notified_on")
        self.evening = saved.get("evening")
        # Prima dei piani esisteva solo il livello pieno
        saved_plans = saved.get("plans") or {"night": {"percent": saved.get("full_target", 100)}}
        for key, plan in saved_plans.items():
            if key in self.plans:
                self.plans[key].update(plan)
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
                "sun_notified_on": self.sun_notified_on,
                "evening": self.evening,
                "plans": self.plans,
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

    def _current_evening(self, now: datetime) -> dict | None:
        """Domanda della notte che sta per arrivare o di quella in corso, fino all'inizio del giorno.

        La domanda di ieri sera resta salvata (serve a ripristinare il livello quando l'auto
        riparte), ma di giorno non va più mostrata né accetta risposte: una scelta fatta lì
        finirebbe sulla notte già passata.
        """
        q = self.evening
        if not q:
            return None
        today = now.date().isoformat()
        if q["day"] > today or (q["day"] == today and now.time() < self.settings.day_start):
            return q
        return None

    def _active_plan(self) -> str | None:
        """Piano in vigore su Octopus: None quando la carica è sospesa o esclusa per stanotte."""
        q = self.evening
        if not q or q.get("restored") or q.get("day", "") < datetime.now().date().isoformat():
            return "night"
        return q["choice"] if q.get("choice") in self.plans else None

    def set_target(self, plan: str = "night", percent: int | None = None, ready_time: str | None = None) -> None:
        """Livello di carica e ora di fine di uno dei due piani notturni."""
        if plan not in self.plans:
            raise ValueError("piano non valido")
        with self._lock:
            saved = self.plans[plan]
            percent = percent if percent is not None else saved["percent"]
            ready_time = ready_time or saved["time"] or "09:00"
            if not MIN_TARGET <= percent <= 100 or ready_time not in READY_TIMES:
                raise ValueError("livello o orario non ammessi")
            saved.update(percent=percent, time=ready_time)
            title = f"{PLAN_NAMES[plan]}: {percent}% entro le {ready_time}"
            if self._active_plan() != plan:
                # Non è il piano in vigore: resta da parte per la prossima volta che viene scelto
                self._event("bedtime", title, "Salvata, vale da quando la scegli")
            elif self.settings.live:
                self.octopus.set_target(self.octopus.vehicle().device_id, percent, ready_time)
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
        """Dopo le 18, con l'auto collegata, chiede come comportarsi per la notte."""
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        asking = self.evening and self.evening.get("day") == tomorrow
        reduced = self.evening and self.evening.get("choice") in ("none", "home") and not self.evening.get("restored")
        if reduced and not vehicle.plugged and now.date().isoformat() >= self.evening["day"]:
            # La notte è passata e l'auto è ripartita: il livello ridotto non deve valere
            # anche per un rientro a notte fonda, quando la domanda non viene posta
            night = self.plans["night"]
            if self.settings.live:
                self.octopus.set_target(vehicle.device_id, night["percent"], night["time"] or vehicle.target_time or "09:00")
            self.evening["restored"] = True
            self._event("bedtime", f"Carica notturna di nuovo al {night['percent']}%", "L'auto è ripartita")
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
            self._event("help", "Domanda della sera", "Nessuna carica, domani a casa o automatico?")
            if suggestion == "home":
                body = f"Domani di solito sei a casa: previsti fino a {home_kwh:.1f} kWh dal sole. Tocca per scegliere.".replace(".", ",", 1)
            else:
                body = "Come carico stanotte? Tocca per scegliere, altrimenti alle 22 parte la carica automatica."
            self._notify("Auto collegata", body)
            return
        if self.evening.get("choice") is not None:
            return
        if now.hour >= self.settings.evening_default_hour:
            # Nessuna risposta: si applica il suggerimento
            self._apply_evening(self.evening["suggestion"], by_user=False)
        elif not self.evening.get("waiting"):
            # Finché manca la risposta Octopus non deve partire col livello della sera prima
            self.evening["waiting"] = True
            if self.settings.live:
                self.octopus.set_target(vehicle.device_id, MIN_TARGET, vehicle.target_time or "09:00")
            self._event("hourglass_top", "Carica notturna sospesa", "In attesa della tua risposta, fino alle 22")

    def _apply_evening(self, choice: str, by_user: bool) -> None:
        plan = self.plans.get(choice)
        vehicle = self.octopus.vehicle()
        # Senza piano è "nessuna carica": il livello va sotto quello dell'auto e non parte nulla
        percent = plan["percent"] if plan else MIN_TARGET
        ready = (plan or self.plans["night"])["time"] or vehicle.target_time or "09:00"
        if self.settings.live:
            self.octopus.set_target(vehicle.device_id, percent, ready)
        self.evening.update(choice=choice, by_user=by_user, percent=percent, time=ready, restored=False)
        self.history.log_evening(self.evening)
        who = "Scelto da te" if by_user else "Nessuna risposta: ho seguito il suggerimento"
        if plan is None:
            self._event("block", "Stanotte nessuna carica", who)
        else:
            icon = "wb_sunny" if choice == "home" else "bedtime"
            self._event(icon, f"{PLAN_NAMES[choice]}: stanotte carico fino al {percent}% entro le {ready}", who)
        if not by_user:
            self._notify(
                "Ho deciso io per stanotte",
                f"{PLAN_NAMES[choice]}: carico fino al {percent}% entro le {ready}. Puoi cambiare dall'app.",
            )

    def answer_evening(self, choice: str) -> None:
        if choice not in ("none", "home", "night"):
            raise ValueError("scelta non valida")
        with self._lock:
            if not self._current_evening(datetime.now()):
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
                "evening": self._current_evening(datetime.now()),
                "plans": self.plans,
                "active_plan": self._active_plan(),
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

    def _no_charge_today(self, now: datetime) -> bool:
        """Ieri sera è stato scelto «Nessuna carica»: vale anche per il sole di oggi."""
        q = self.evening
        return bool(q and q.get("day") == now.date().isoformat() and q.get("choice") == "none")

    def _sun_notice(self, now: datetime, plant, vehicle) -> None:
        """Una volta al giorno, quando il sole arriva a bastare per l'auto.

        Tre casi. Con l'auto collegata e «Carica col sole» attiva la carica parte da sola e
        arriva la notifica di avvio, qui non serve nulla. Con l'auto collegata ma la scelta
        «Nessuna carica» della sera prima, si avvisa che il sole c'è e si lascia decidere.
        Con l'auto scollegata si avvisa di collegarla, ma solo nelle ore in cui di solito
        è a casa (HOME_PLAN): a chi è al lavoro il sole sul tetto non serve.
        """
        today = now.date().isoformat()
        in_day = self.settings.day_start <= now.time() < self.settings.day_end
        if self.sun_notified_on == today or not in_day or not enough_sun(plant, self.settings):
            return
        self.sun_notified_on = today
        self._event("sunny", "Sole sufficiente per l'auto", f"Pannelli a {plant.pv_w:.0f} W, sopra il minimo")
        if vehicle.plugged and self.mode is not Mode.AUTO:
            return
        if not vehicle.plugged:
            plan = _home_plan()
            if plan and not any(start <= now.hour < end for start, end in plan.get(now.weekday(), ())):
                return
        kw = f"{plant.pv_w / 1000:.1f}".replace(".", ",")
        end = self.settings.day_end.strftime("%H:%M")
        if vehicle.plugged:
            body = f"Pannelli a {kw} kW. Avevi scelto di non caricare: se vuoi approfittarne, tocca e scegli «Carica col sole»."
        elif self.mode is Mode.SOLAR:
            body = f"Pannelli a {kw} kW. Collega l'auto e la carica col sole parte da sola, fino alle {end}."
        else:
            body = f"Pannelli a {kw} kW. Collega l'auto e scegli «Carica col sole» per caricare fino alle {end}."
        self._notify("C'è sole per caricare l'auto", body)

    def _auto_switch(self, now: datetime, ours: bool) -> None:
        """Passaggi automatici: al mattino a "carica col sole", la sera ad "automatica"."""
        today = now.date().isoformat()
        in_day = self.settings.day_start <= now.time() < self.settings.day_end
        if in_day and self.switched_on != today:
            self.switched_on = today
            if self.mode is Mode.AUTO and self._no_charge_today(now):
                # «Nessuna carica» vale anche di giorno: niente avvio automatico col sole,
                # quando il sole basta arriva un avviso e si decide dall'app
                self._event("block", "Oggi nessuna carica automatica", "Scelta ieri sera: ti avviso quando c'è sole")
            elif self.mode is Mode.AUTO:
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

    def _check_skipped_night(self, now: datetime, vehicle, windows: dict | None) -> None:
        """Avvisa se Octopus ha in programma una carica in una notte in cui si è scelto di non caricare.

        Octopus fissa il piano della notte poco dopo il collegamento, con il livello di quel
        momento, e abbassandolo dopo non lo ricalcola: nelle notti rispettate lo stato restava
        "capable", in quella non rispettata era "in progress" con una finestra in programma.
        Il comando viene ripetuto e l'utente avvisato una volta, perché solo dall'auto o
        dall'app di Octopus la carica si ferma con certezza.
        """
        q = self._current_evening(now)
        if not q or q.get("choice") != "none" or q.get("warned") or not windows or vehicle.state != STATE_PLANNED:
            return
        q["warned"] = True
        if self.settings.live:
            self.octopus.set_target(vehicle.device_id, MIN_TARGET, q.get("time") or vehicle.target_time or "09:00")
        body = (
            f"Octopus ha già in programma una carica dalle {windows['start']} alle {windows['end']} "
            "e potrebbe non rispettare «Nessuna carica». Per esserne sicuro fermala dall'app Tesla quando parte."
        )
        self._event("error", "Octopus ha in programma una carica", body)
        self._notify("Octopus ha in programma una carica", body)

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
        self._confirm_notice(now, vehicle)
        for plan in self.plans.values():
            # Al primo giro i piani prendono l'ora di fine carica già impostata su Octopus
            plan["time"] = plan["time"] or vehicle.target_time or "09:00"
        self._sync_dispatches(now)
        self.status["planned"] = self._planned_window(vehicle.device_id, now)
        self._check_skipped_night(now, vehicle, self.status["planned"])
        self._auto_switch(now, vehicle.boosting and self.state.started_by_us)
        self._sun_notice(now, plant, vehicle)

        if not vehicle.plugged:
            self._car_stale = True
        hints = (vehicle.boosting, vehicle.plugged, self._grid_ok(now))
        car_full = self._car_full(now)
        need_car = precheck(now, self.mode, plant, *hints, self.state, self.settings, car_full) is None
        # Appena l'auto viene collegata è sveglia: una lettura costa poco e aggiorna il pannello
        just_plugged = vehicle.plugged and self._was_plugged is False
        just_unplugged = not vehicle.plugged and self._was_plugged is True
        self._was_plugged = vehicle.plugged
        if just_plugged:
            self._plugged_at = now
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
        in_day = self.settings.day_start <= now.time() < self.settings.day_end
        if in_day and self.mode is Mode.AUTO and decision.action is Action.HOLD and self._no_charge_today(now):
            decision = replace(decision, reason="oggi nessuna carica automatica, come scelto ieri sera: ti avviso quando c'è sole")
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
            # Solo nell'app, mai come notifica: con l'auto collegata la carica parte da sola
            # appena il sole basta, e chi vuole caricare da rete lo stesso lo sceglie da lì.
            self.asked_on = now.date().isoformat()
            self._event("notifications", "Richiesta di consenso", f"Pannelli a {plant.pv_w:.0f} W, sotto il minimo")
        if decision.action is Action.START and self._plugged_at and now - self._plugged_at < PLUG_SETTLE:
            # Il 9 ottobre una carica chiesta sei secondi dopo il collegamento è stata annullata da Octopus
            log.info("auto appena collegata: avvio rimandato al prossimo ciclo")
            return
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
        # Il comando è partito, ma l'auto ci mette un po': la notifica aspetta la conferma
        if decision.action is Action.START:
            self._notice = {
                "boosting": True, "since": now,
                "title": "C'è sole, carica avviata" if self.mode is Mode.SOLAR else "Carica avviata",
                "body": f"{decision.amps} A all'auto. {reason}",
            }
        elif decision.action is Action.STOP:
            self._notice = {"boosting": False, "since": now, "title": "Carica fermata", "body": reason}
            if self.mode is Mode.BOOST:
                self.mode = self.after_boost

    def _confirm_notice(self, now: datetime, vehicle) -> None:
        """Manda la notifica di avvio o di stop solo quando Octopus conferma che è avvenuto."""
        notice = self._notice
        if notice is None:
            return
        if vehicle.boosting == notice["boosting"]:
            self._notify(notice["title"], notice["body"])
        elif now - notice["since"] < NOTICE_TIMEOUT:
            return
        else:
            title = "Carica non ancora avviata" if notice["boosting"] else "Carica non ancora fermata"
            body = f"Comando inviato alle {notice['since']:%H:%M}, ma Octopus non lo conferma."
            if notice["boosting"]:
                # L'avvio non è avvenuto: niente attesa tra due manovre, si riprova appena possibile
                self.state = replace(self.state, started_by_us=False, last_switch=None)
                body += " Riprovo al prossimo ciclo."
            self._event("error", title, body)
            self._notify(title, body)
        self._notice = None

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
