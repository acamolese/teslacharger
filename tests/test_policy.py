import unittest
from datetime import datetime, time, timedelta

from teslacharger.config import Settings
from teslacharger.policy import Action, ControlState, decide
from teslacharger.solax import PlantSnapshot

SETTINGS = Settings(
    car_power_w=2800,
    battery_assist_w=500,
    soc_start=80,
    soc_stop=50,
    deficit_samples=2,
    min_switch_minutes=15,
    day_start=time(9),
    day_end=time(18),
    poll_seconds=300,
)
NOON = datetime(2026, 10, 3, 12, 0)


def plant(battery_w=0, grid_w=0, soc=90, data_time="12:00"):
    return PlantSnapshot(
        data_time=data_time, pv_w=4000, inverter_ac_w=2000,
        grid_w=grid_w, battery_w=battery_w, battery_soc=soc,
    )


class StartTests(unittest.TestCase):
    def test_starts_with_enough_surplus(self):
        d = decide(NOON, plant(battery_w=2500), False, ControlState(), SETTINGS)
        self.assertEqual(d.action, Action.START)
        self.assertTrue(d.state.started_by_us)

    def test_counts_export_as_surplus(self):
        d = decide(NOON, plant(battery_w=0, grid_w=2400, soc=100), False, ControlState(), SETTINGS)
        self.assertEqual(d.action, Action.START)

    def test_holds_with_small_surplus(self):
        d = decide(NOON, plant(battery_w=1900), False, ControlState(), SETTINGS)
        self.assertEqual(d.action, Action.HOLD)

    def test_holds_when_home_battery_low(self):
        d = decide(NOON, plant(battery_w=3000, soc=60), False, ControlState(), SETTINGS)
        self.assertEqual(d.action, Action.HOLD)

    def test_holds_at_night(self):
        d = decide(NOON.replace(hour=20), plant(battery_w=3000), False, ControlState(), SETTINGS)
        self.assertEqual(d.action, Action.HOLD)

    def test_waits_between_switches(self):
        state = ControlState(last_switch=NOON - timedelta(minutes=5))
        d = decide(NOON, plant(battery_w=3000), False, state, SETTINGS)
        self.assertEqual(d.action, Action.HOLD)


class StopTests(unittest.TestCase):
    OURS = ControlState(started_by_us=True, last_switch=NOON - timedelta(hours=1))

    def test_never_touches_manual_boost(self):
        d = decide(NOON.replace(hour=20), plant(battery_w=-3000, soc=20), True, ControlState(), SETTINGS)
        self.assertEqual(d.action, Action.HOLD)

    def test_keeps_charging_within_battery_assist(self):
        d = decide(NOON, plant(battery_w=-400), True, self.OURS, SETTINGS)
        self.assertEqual(d.action, Action.HOLD)

    def test_stops_after_consecutive_deficits(self):
        d1 = decide(NOON, plant(battery_w=-900, data_time="12:00"), True, self.OURS, SETTINGS)
        self.assertEqual(d1.action, Action.HOLD)
        d2 = decide(NOON, plant(battery_w=-900, data_time="12:05"), True, d1.state, SETTINGS)
        self.assertEqual(d2.action, Action.STOP)
        self.assertFalse(d2.state.started_by_us)

    def test_same_reading_is_not_counted_twice(self):
        d1 = decide(NOON, plant(battery_w=-900, data_time="12:00"), True, self.OURS, SETTINGS)
        d2 = decide(NOON, plant(battery_w=-900, data_time="12:00"), True, d1.state, SETTINGS)
        self.assertEqual(d2.action, Action.HOLD)

    def test_deficit_count_resets_when_sun_returns(self):
        d1 = decide(NOON, plant(battery_w=-900, data_time="12:00"), True, self.OURS, SETTINGS)
        d2 = decide(NOON, plant(battery_w=200, data_time="12:05"), True, d1.state, SETTINGS)
        d3 = decide(NOON, plant(battery_w=-900, data_time="12:10"), True, d2.state, SETTINGS)
        self.assertEqual(d3.action, Action.HOLD)

    def test_stops_when_home_battery_too_low(self):
        d = decide(NOON, plant(battery_w=0, soc=45), True, self.OURS, SETTINGS)
        self.assertEqual(d.action, Action.STOP)

    def test_stops_at_end_of_day_even_right_after_start(self):
        state = ControlState(started_by_us=True, last_switch=NOON.replace(hour=17, minute=55))
        d = decide(NOON.replace(hour=18, minute=1), plant(battery_w=500), True, state, SETTINGS)
        self.assertEqual(d.action, Action.STOP)


if __name__ == "__main__":
    unittest.main()
