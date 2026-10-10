"""Simulazione del controller: carica partita dall'auto senza Octopus, come il 10 ottobre."""

import logging
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from teslacharger.controller import Controller
from teslacharger.history import History
from teslacharger.octopus import VehicleStatus
from teslacharger.policy import CarStatus
from teslacharger.solax import PlantSnapshot

from test_policy import SETTINGS

PLUG = datetime(2026, 10, 10, 18, 9)
logging.getLogger("teslacharger").setLevel(logging.CRITICAL)


class FakeSolax:
    def __init__(self):
        self.load = 200

    def snapshot(self):
        return PlantSnapshot(
            data_time=datetime.now().isoformat(), pv_w=0, inverter_ac_w=0, grid_w=0,
            battery_w=-self.load, battery_soc=70,
        )


class FakeOctopus:
    def __init__(self):
        self.state = "SMART_CONTROL_CAPABLE"
        self.windows = []

    def vehicle(self):
        return VehicleStatus("dev", "auto", self.state, False, target_percent=50, target_time="09:00")

    def planned_dispatches(self, device_id):
        return self.windows

    def completed_dispatches(self):
        return []

    def charging_sessions(self, limit=10):
        return []


class FakeCar:
    def __init__(self):
        self.charging = False
        self.level = 71
        self.reads = 0
        self.amps = 12
        self.stops = 0
        self.last_info = None

    def status(self):
        self.reads += 1
        self.last_info = {"level": self.level, "limit": 100, "charging": self.charging}
        return CarStatus(True, self.charging, self.level, 100, self.amps, 13, 230 if self.charging else 2)

    def set_amps(self, amps):
        self.amps = amps

    def stop_charging(self):
        self.stops += 1
        self.charging = False


class FakePush:
    enabled = False


class StrayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TESLACHARGER_DATA"] = self.tmp.name
        self.solax, self.octopus, self.car = FakeSolax(), FakeOctopus(), FakeCar()
        self.c = Controller(
            replace(SETTINGS, live=True), solax=self.solax, octopus=self.octopus, car=self.car,
            history=History(Path(self.tmp.name) / "h.db"), push=FakePush(),
        )
        self.c.forecast_summary = lambda: {"days": []}
        self.c._was_plugged = False
        # La domanda della sera è già stata risposta: domani a casa, 50%
        self.c.evening = {"day": "2026-10-11", "choice": "home", "suggestion": "home", "percent": 50}

    def tearDown(self):
        self.tmp.cleanup()

    def cycle(self, minutes):
        self.c._cycle(PLUG + timedelta(minutes=minutes))

    def test_stops_the_car_and_confirms(self):
        self.cycle(0)
        self.assertEqual(self.car.reads, 1)  # lettura al collegamento
        self.car.charging, self.solax.load = True, 2700
        self.cycle(5)  # il consumo sale: si legge l'auto e la si ferma
        self.assertEqual(self.car.stops, 1)
        self.solax.load = 200
        self.cycle(10)  # conferma
        titles = [e["title"] for e in self.c.events]
        self.assertIn("Carica fermata", titles)
        self.assertNotIn("pending", self.c._stray)

    def test_no_reads_while_load_is_low(self):
        self.cycle(0)
        self.c._stray["settled"] = True
        for m in range(5, 120, 5):
            self.cycle(m)
        self.assertEqual(self.car.reads, 1)

    def test_settle_read_catches_charge_with_high_load_already(self):
        self.solax.load = 2500  # la casa consumava già molto prima del collegamento
        self.cycle(0)
        self.car.charging = True
        self.cycle(5)
        self.assertEqual(self.car.stops, 0)
        self.cycle(10)  # lettura dopo l'assestamento
        self.assertEqual(self.car.stops, 1)

    def test_octopus_window_is_respected(self):
        self.cycle(0)
        start = (PLUG + timedelta(minutes=1)).astimezone().isoformat()
        end = (PLUG + timedelta(hours=2)).astimezone().isoformat()
        self.octopus.windows = [{"start": start, "end": end, "type": "SMART"}]
        self.car.charging, self.solax.load = True, 2700
        self.cycle(5)
        self.assertEqual(self.car.stops, 0)

    def test_gives_up_after_two_stops(self):
        self.cycle(0)
        for m in (5, 10, 15, 20, 25, 30):
            self.car.charging, self.solax.load = True, 2700
            self.c._stray["checked"] = None
            self.c._stray["high"] = False
            self.cycle(m)
        self.assertEqual(self.car.stops, 2)
        self.assertTrue(self.c._stray.get("gave_up"))

    def test_low_current_is_restored_once(self):
        self.car.amps = 7
        self.cycle(0)
        self.assertEqual(self.car.amps, 12)
        self.car.amps = 7
        self.cycle(10)
        self.assertEqual(self.car.amps, 7)


if __name__ == "__main__":
    unittest.main()
