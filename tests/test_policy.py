import unittest
from datetime import datetime, time, timedelta

from teslacharger.config import Settings
from teslacharger.policy import Action, CarStatus, ControlState, Mode, decide, precheck
from teslacharger.solax import PlantSnapshot

SETTINGS = Settings(
    min_amps=5,
    battery_assist_w=300,
    soc_start=80,
    soc_stop=50,
    deficit_samples=2,
    min_switch_minutes=15,
    day_start=time(9),
    day_end=time(18),
    poll_seconds=150,
    car_retry_minutes=30,
    live=False,
)
NOON = datetime(2026, 10, 3, 12, 0)
OURS = ControlState(started_by_us=True, last_switch=NOON - timedelta(hours=1))


def plant(excess=0, soc=90, data_time="12:00"):
    return PlantSnapshot(
        data_time=data_time, pv_w=4000, inverter_ac_w=2000,
        grid_w=0, battery_w=excess, battery_soc=soc,
    )


def car(charging=False, amps=13, level=60, plugged=True, limit=100):
    return CarStatus(
        plugged=plugged, charging=charging, level=level, limit=limit,
        amps=amps, max_amps=13, voltage=230 if charging else 2,
    )


def run(p, c, boosting=False, state=ControlState(), mode=Mode.AUTO, now=NOON):
    return decide(now, mode, p, c, boosting, state, SETTINGS)


class PrecheckTests(unittest.TestCase):
    """Casi in cui si decide senza interrogare l'auto."""

    def check(self, p, boosting=False, state=ControlState(), mode=Mode.AUTO, now=NOON):
        return precheck(now, mode, p, boosting, state, SETTINGS)

    def test_car_not_needed_without_surplus(self):
        self.assertEqual(self.check(plant(excess=600)).action, Action.HOLD)

    def test_car_not_needed_at_night(self):
        self.assertEqual(self.check(plant(excess=3000), now=NOON.replace(hour=21)).action, Action.HOLD)

    def test_car_not_needed_when_home_battery_has_priority(self):
        self.assertEqual(self.check(plant(excess=3000, soc=60)).action, Action.HOLD)

    def test_car_not_needed_when_off(self):
        self.assertEqual(self.check(plant(excess=3000), mode=Mode.OFF).action, Action.HOLD)

    def test_manual_boost_is_left_alone(self):
        d = self.check(plant(excess=-3000, soc=20), boosting=True, now=NOON.replace(hour=21))
        self.assertEqual(d.action, Action.HOLD)

    def test_car_needed_with_surplus(self):
        self.assertIsNone(self.check(plant(excess=2000)))

    def test_recently_unplugged_car_is_not_polled_again(self):
        state = ControlState(unplugged_at=NOON - timedelta(minutes=10))
        self.assertEqual(self.check(plant(excess=2000), state=state).action, Action.HOLD)
        state = ControlState(unplugged_at=NOON - timedelta(minutes=40))
        self.assertIsNone(self.check(plant(excess=2000), state=state))


class AutoStartTests(unittest.TestCase):
    def test_starts_at_amps_matching_surplus(self):
        d = run(plant(excess=1800), car())
        self.assertEqual(d.action, Action.START)
        self.assertEqual(d.amps, 9)  # (1800 + 300 di aiuto) / 230
        self.assertTrue(d.state.started_by_us)

    def test_amps_capped_at_car_maximum(self):
        self.assertEqual(run(plant(excess=5000), car()).amps, 13)

    def test_wakes_sleeping_car_once(self):
        d = run(plant(excess=2000), None)
        self.assertEqual(d.action, Action.WAKE)
        self.assertEqual(run(plant(excess=2000), None, state=d.state).action, Action.HOLD)

    def test_holds_when_unplugged_and_remembers_it(self):
        d = run(plant(excess=2000), car(plugged=False))
        self.assertEqual(d.action, Action.HOLD)
        self.assertEqual(d.state.unplugged_at, NOON)

    def test_holds_when_car_is_full(self):
        self.assertEqual(run(plant(excess=2000), car(level=100)).action, Action.HOLD)

    def test_waits_between_switches(self):
        state = ControlState(last_switch=NOON - timedelta(minutes=5))
        self.assertEqual(run(plant(excess=2000), car(), state=state).action, Action.HOLD)


class AutoRegulationTests(unittest.TestCase):
    def test_raises_amps_when_sun_increases(self):
        # L'auto assorbe 8 A (1840 W) e avanzano ancora 700 W: può salire a 12 A
        d = run(plant(excess=700), car(charging=True, amps=8), boosting=True, state=OURS)
        self.assertEqual((d.action, d.amps), (Action.SET_AMPS, 12))

    def test_lowers_amps_when_sun_decreases(self):
        # L'auto assorbe 13 A (2990 W) ma casa è in deficit di 1000 W
        d = run(plant(excess=-1000), car(charging=True, amps=13), boosting=True, state=OURS)
        self.assertEqual((d.action, d.amps), (Action.SET_AMPS, 9))

    def test_holds_when_amps_already_right(self):
        d = run(plant(excess=0), car(charging=True, amps=9), boosting=True, state=OURS)
        self.assertEqual(d.action, Action.SET_AMPS)  # con l'aiuto della batteria sale a 10
        d = run(plant(excess=-300), car(charging=True, amps=9), boosting=True, state=OURS)
        self.assertEqual(d.action, Action.HOLD)

    def test_same_reading_is_not_acted_on_twice(self):
        state = ControlState(started_by_us=True, last_data_time="12:00")
        d = run(plant(excess=700), car(charging=True, amps=8), boosting=True, state=state)
        self.assertEqual(d.action, Action.HOLD)

    def test_drops_to_minimum_then_stops_after_consecutive_deficits(self):
        d1 = run(plant(excess=-1500, data_time="12:00"), car(charging=True, amps=8), boosting=True, state=OURS)
        self.assertEqual((d1.action, d1.amps), (Action.SET_AMPS, 5))
        d2 = run(plant(excess=-900, data_time="12:05"), car(charging=True, amps=5), boosting=True, state=d1.state)
        self.assertEqual(d2.action, Action.STOP)
        self.assertEqual(d2.amps, 13)  # ripristina il massimo per la carica notturna
        self.assertFalse(d2.state.started_by_us)

    def test_deficit_count_resets_when_sun_returns(self):
        d1 = run(plant(excess=-1500, data_time="12:00"), car(charging=True, amps=8), boosting=True, state=OURS)
        d2 = run(plant(excess=500, data_time="12:05"), car(charging=True, amps=5), boosting=True, state=d1.state)
        self.assertEqual(d2.state.deficit_count, 0)

    def test_stops_when_home_battery_too_low(self):
        d = run(plant(excess=0, soc=45), car(charging=True, amps=8), boosting=True, state=OURS)
        self.assertEqual(d.action, Action.STOP)

    def test_stops_at_end_of_day(self):
        d = run(plant(excess=500), car(charging=True, amps=8), boosting=True, state=OURS,
                now=NOON.replace(hour=18, minute=1))
        self.assertEqual(d.action, Action.STOP)

    def test_stops_when_car_is_full(self):
        d = run(plant(excess=500), car(charging=True, amps=8, level=100), boosting=True, state=OURS)
        self.assertEqual(d.action, Action.STOP)

    def test_stops_when_unplugged(self):
        d = run(plant(excess=500), car(plugged=False), boosting=True, state=OURS)
        self.assertEqual(d.action, Action.STOP)


class ModeTests(unittest.TestCase):
    def test_boost_starts_at_full_power_even_without_sun(self):
        d = run(plant(excess=-500, soc=30), car(), mode=Mode.BOOST, now=NOON.replace(hour=21))
        self.assertEqual((d.action, d.amps), (Action.START, 13))

    def test_boost_raises_amps_to_maximum(self):
        d = run(plant(), car(charging=True, amps=6), boosting=True, mode=Mode.BOOST)
        self.assertEqual((d.action, d.amps), (Action.SET_AMPS, 13))

    def test_off_releases_our_charge(self):
        d = run(plant(excess=2000), car(charging=True, amps=8), boosting=True, state=OURS, mode=Mode.OFF)
        self.assertEqual(d.action, Action.STOP)

    def test_off_leaves_manual_boost_alone(self):
        d = run(plant(), car(charging=True), boosting=True, mode=Mode.OFF)
        self.assertEqual(d.action, Action.HOLD)


class StateTests(unittest.TestCase):
    def test_state_survives_json_round_trip(self):
        state = ControlState(started_by_us=True, last_switch=NOON, deficit_count=1, last_data_time="x")
        self.assertEqual(ControlState.from_json(state.to_json()), state)


if __name__ == "__main__":
    unittest.main()
