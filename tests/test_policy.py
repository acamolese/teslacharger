import unittest
from datetime import datetime, time, timedelta

from teslacharger.config import Settings
from teslacharger.policy import Action, CarStatus, ControlState, Mode, decide, precheck
from teslacharger.solax import PlantSnapshot

SETTINGS = Settings(
    min_amps=5,
    pv_share=80,
    deficit_samples=2,
    min_switch_minutes=15,
    day_start=time(9),
    day_end=time(18),
    poll_seconds=150,
    car_retry_minutes=30,
    live=False,
)
NOON = datetime(2026, 10, 3, 12, 0)
NIGHT = NOON.replace(hour=21)
OURS = ControlState(started_by_us=True, last_switch=NOON - timedelta(hours=1))


def plant(pv=2000, soc=90, data_time="12:00", excess=0):
    return PlantSnapshot(
        data_time=data_time, pv_w=pv, inverter_ac_w=2000,
        grid_w=0, battery_w=excess, battery_soc=soc,
    )


def car(charging=False, amps=13, level=60, plugged=True, limit=100):
    return CarStatus(
        plugged=plugged, charging=charging, level=level, limit=limit,
        amps=amps, max_amps=13, voltage=230 if charging else 2,
    )


def run(p, c, boosting=False, state=ControlState(), mode=Mode.AUTO, now=NOON, plugged=True, grid_ok=False):
    return decide(now, mode, p, c, boosting, plugged, grid_ok, state, SETTINGS)


class PrecheckTests(unittest.TestCase):
    """Casi in cui si decide senza interrogare l'auto."""

    def check(self, p, boosting=False, state=ControlState(), mode=Mode.AUTO, now=NOON, plugged=True, grid_ok=False):
        return precheck(now, mode, p, boosting, plugged, grid_ok, state, SETTINGS)

    def test_car_not_needed_at_night(self):
        self.assertEqual(self.check(plant(excess=3000), now=NIGHT).action, Action.HOLD)

    def test_car_not_needed_when_octopus_says_unplugged(self):
        self.assertEqual(self.check(plant(excess=3000), plugged=False).action, Action.HOLD)

    def test_car_not_needed_when_off(self):
        self.assertEqual(self.check(plant(excess=3000), mode=Mode.OFF).action, Action.HOLD)

    def test_manual_boost_is_left_alone(self):
        d = self.check(plant(excess=-3000, soc=20), boosting=True, now=NIGHT)
        self.assertEqual(d.action, Action.HOLD)

    def test_car_needed_in_daytime_with_sun(self):
        self.assertIsNone(self.check(plant(pv=2000)))

    def test_asks_for_consent_without_sun_and_does_not_poll_the_car(self):
        d = self.check(plant(pv=800))
        self.assertEqual(d.action, Action.HOLD)
        self.assertTrue(d.ask)

    def test_car_needed_without_sun_once_consent_is_given(self):
        self.assertIsNone(self.check(plant(pv=800), grid_ok=True))

    def test_does_not_ask_when_car_is_away(self):
        self.assertFalse(self.check(plant(pv=800), plugged=False).ask)

    def test_idle_car_is_not_polled_again_too_soon(self):
        state = ControlState(idle_at=NOON - timedelta(minutes=10))
        self.assertEqual(self.check(plant(excess=2000), state=state).action, Action.HOLD)
        state = ControlState(idle_at=NOON - timedelta(minutes=40))
        self.assertIsNone(self.check(plant(excess=2000), state=state))

    def test_car_polled_only_on_fresh_data_while_charging(self):
        state = ControlState(started_by_us=True, last_data_time="12:00")
        self.assertEqual(self.check(plant(data_time="12:00"), boosting=True, state=state).action, Action.HOLD)
        self.assertIsNone(self.check(plant(data_time="12:05"), boosting=True, state=state))

    def test_car_needed_to_stop_our_charge_at_night(self):
        self.assertIsNone(self.check(plant(), boosting=True, state=OURS, now=NIGHT))


class AutoStartTests(unittest.TestCase):
    def test_starts_with_80_percent_of_panels(self):
        # 2000 W dai pannelli: all'auto 1600 W, cioè 6 A
        d = run(plant(pv=2000), car())
        self.assertEqual((d.action, d.amps), (Action.START, 6))
        self.assertTrue(d.state.started_by_us)

    def test_starts_at_minimum_without_sun_only_with_consent(self):
        d = run(plant(pv=100), car(), grid_ok=True)
        self.assertEqual((d.action, d.amps), (Action.START, 5))
        d = run(plant(pv=100), car())
        self.assertEqual(d.action, Action.HOLD)
        self.assertTrue(d.ask)

    def test_amps_capped_at_car_maximum(self):
        self.assertEqual(run(plant(pv=6000), car()).amps, 13)

    def test_minimum_sun_needed_is_about_1400_watts(self):
        # 5 A a 230 V sono 1150 W, cioè l'80% di circa 1440 W
        self.assertTrue(run(plant(pv=1400), car()).ask)
        self.assertEqual(run(plant(pv=1450), car()).amps, 5)

    def test_wakes_sleeping_car_once(self):
        d = run(plant(), None)
        self.assertEqual(d.action, Action.WAKE)
        self.assertEqual(run(plant(), None, state=d.state).action, Action.HOLD)

    def test_holds_when_unplugged_and_remembers_it(self):
        d = run(plant(), car(plugged=False))
        self.assertEqual(d.action, Action.HOLD)
        self.assertEqual(d.state.idle_at, NOON)

    def test_holds_when_car_is_full(self):
        d = run(plant(), car(level=100))
        self.assertEqual(d.action, Action.HOLD)
        self.assertEqual(d.state.idle_at, NOON)

    def test_waits_between_switches(self):
        state = ControlState(last_switch=NOON - timedelta(minutes=5))
        self.assertEqual(run(plant(), car(), state=state).action, Action.HOLD)


class AutoRegulationTests(unittest.TestCase):
    def test_raises_amps_when_sun_increases(self):
        # 3500 W dai pannelli: all'auto 2800 W, cioè 12 A
        d = run(plant(pv=3500), car(charging=True, amps=8), boosting=True, state=OURS)
        self.assertEqual((d.action, d.amps), (Action.SET_AMPS, 12))

    def test_lowers_amps_when_sun_decreases(self):
        d = run(plant(pv=2000), car(charging=True, amps=13), boosting=True, state=OURS)
        self.assertEqual((d.action, d.amps), (Action.SET_AMPS, 6))

    def test_holds_when_amps_already_right(self):
        d = run(plant(pv=2000), car(charging=True, amps=6), boosting=True, state=OURS)
        self.assertEqual(d.action, Action.HOLD)

    def test_with_consent_stays_at_minimum_without_sun(self):
        state = OURS
        for minute in ("12:00", "12:05", "12:10"):
            d = run(plant(pv=0, data_time=minute), car(charging=True, amps=5), boosting=True, state=state, grid_ok=True)
            self.assertEqual(d.action, Action.HOLD)
            state = d.state

    def test_without_consent_drops_to_minimum_then_stops_and_asks(self):
        d1 = run(plant(pv=500, data_time="12:00"), car(charging=True, amps=9), boosting=True, state=OURS)
        self.assertEqual((d1.action, d1.amps), (Action.SET_AMPS, 5))
        d2 = run(plant(pv=500, data_time="12:05"), car(charging=True, amps=5), boosting=True, state=d1.state)
        self.assertEqual((d2.action, d2.amps), (Action.STOP, 13))
        self.assertTrue(d2.ask)

    def test_passing_cloud_does_not_stop_the_charge(self):
        d1 = run(plant(pv=500, data_time="12:00"), car(charging=True, amps=9), boosting=True, state=OURS)
        d2 = run(plant(pv=3500, data_time="12:05"), car(charging=True, amps=5), boosting=True, state=d1.state)
        self.assertEqual((d2.action, d2.amps), (Action.SET_AMPS, 12))
        self.assertEqual(d2.state.deficit_count, 0)

    def test_same_reading_is_not_acted_on_twice(self):
        state = ControlState(started_by_us=True, last_data_time="12:00")
        d = run(plant(pv=3500), car(charging=True, amps=8), boosting=True, state=state)
        self.assertEqual(d.action, Action.HOLD)

    def test_stops_at_end_of_day_and_restores_maximum_amps(self):
        d = run(plant(), car(charging=True, amps=8), boosting=True, state=OURS,
                now=NOON.replace(hour=18, minute=1))
        self.assertEqual((d.action, d.amps), (Action.STOP, 13))
        self.assertFalse(d.state.started_by_us)

    def test_stops_when_car_is_full(self):
        d = run(plant(), car(charging=True, amps=8, level=100), boosting=True, state=OURS)
        self.assertEqual(d.action, Action.STOP)

    def test_stops_when_unplugged(self):
        d = run(plant(), car(plugged=False), boosting=True, state=OURS)
        self.assertEqual(d.action, Action.STOP)


class ModeTests(unittest.TestCase):
    def test_boost_starts_at_full_power_even_without_sun(self):
        d = run(plant(excess=-500, soc=30), car(), mode=Mode.BOOST, now=NIGHT)
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
        state = ControlState(started_by_us=True, last_switch=NOON, deficit_count=1, last_data_time="x", idle_at=NOON)
        self.assertEqual(ControlState.from_json(state.to_json()), state)

    def test_old_state_file_fields_are_ignored(self):
        self.assertEqual(ControlState.from_json({"soc_seen": 2, "unplugged_at": None}), ControlState())


if __name__ == "__main__":
    unittest.main()
