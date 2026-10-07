from __future__ import annotations

import ast
import asyncio
import os
import unittest

import _bootstrap  # noqa: F401  定位核心仓库并挂 sys.path

from dglab.state import EngineState, Slot
from dglab.waves import SILENT

from modules.margin_control.bridge import (PARAM_DEFS,
                                           EdgeGuard, MarginBridge,
                                           MarginConfig, PHASE_COOL,
                                           PHASE_IDLE, PHASE_RELEASE,
                                           PHASE_STIM)
from modules.margin_control.plugin import (MARGIN_CONFIG_DEFAULTS, META,
                                           MarginControlModule)

_FAST_JUDGE = {"edge_hold_s": 0.0, "recovery_hold_s": 0.0, "jump_rise": 0.0,
               "sensor_timeout_s": 3600.0}


class FakeCommands:

    def __init__(self):
        self.state = EngineState(backend="ble")
        self.state.slots["s_out"] = Slot(slot_id="s_out", name="郊狼",
                                         type="COYOTE_030")
        self.state.slots["s_bmt"] = Slot(slot_id="s_bmt", name="灵猫",
                                         type="BMTR_010")
        self.strength_calls: list[tuple[str, int, str | None]] = []
        self.zap_calls: list[tuple[str, float, str | None]] = []
        self.fire_calls: list[tuple] = []
        self.reset_calls: list[tuple] = []
        self.selection = {"A": SILENT, "B": SILENT}


    def get_state(self):
        return self.state

    def resolve_slot(self, slot_id=None, family=None, output_only=False):
        if slot_id:
            return slot_id
        if family:
            for sid in sorted(self.state.slots):
                slot = self.state.slots[sid]
                if slot.type.upper().startswith(family):
                    return sid
            return None
        for sid in sorted(self.state.slots):
            if not output_only or self.state.slots[sid].is_output_device:
                return sid
        return None

    def wave_selection(self):
        return dict(self.selection)


    def set_strength(self, channel, value, slot_id=None):
        self.strength_calls.append((channel, int(value), slot_id))

        async def _noop():
            pass
        return _noop()

    def set_wave(self, channel, name, slot_id=None):
        async def _noop():
            pass
        return _noop()

    def zap(self, channel, seconds=1.0, slot_id=None):
        self.zap_calls.append((channel, float(seconds), slot_id))

        async def _noop():
            pass
        return _noop()

    def fire(self, *args, **kwargs):
        self.fire_calls.append(args)

        async def _noop():
            pass
        return _noop()

    def fire_start(self, slot_id=None, channel=None):
        self.fire_calls.append(("start", slot_id, channel))

        async def _noop():
            pass
        return _noop()

    def fire_stop(self, slot_id=None, channel=None):
        self.fire_calls.append(("stop", slot_id, channel))

        async def _noop():
            pass
        return _noop()

    def reset_strength(self, channel, slot_id=None):
        self.reset_calls.append((channel, slot_id))

        async def _noop():
            pass
        return _noop()

    def emergency_stop(self):
        async def _noop():
            pass
        return _noop()


def _commands(pressure: float | None = None,
              edge: int | None = None) -> FakeCommands:
    commands = FakeCommands()
    if pressure is not None:
        commands.state.slots["s_bmt"].pressure = pressure
    if edge is not None:
        commands.state.slots["s_bmt"].edge_state = edge
    return commands


_TEST_OUTPUTS = [
    {"param": "BMTR.Pressure", "name": "pressure",
     "expr": "{BMTR.Pressure}", "type": "Float"},
    {"param": "BMTR.EdgeState", "name": "edge",
     "expr": "{BMTR.EdgeState}", "type": "Int"},
]
_TEST_PUSH = [{"name": "推送", "trigger": "period", "arg": 100,
               "actions": [
                   {"dir": "in", "param": "in_strength_a",
                    "var": "stim_strength"},
                   {"dir": "in", "param": "in_strength_b",
                    "var": "stim_strength"}]}]


def _bridge(config: dict | None = None, commands: FakeCommands | None = None,
            clock: float = 100.0) -> tuple[MarginBridge, FakeCommands]:
    commands = commands or _commands()
    merged = dict(_FAST_JUDGE)
    merged.update(config or {})
    if "outputs" not in merged:
        merged["outputs"] = _TEST_OUTPUTS
    if "mappings" not in merged and "events" not in merged:
        merged["events"] = _TEST_PUSH
    bridge = MarginBridge(MarginConfig(merged), commands.get_state, commands)
    bridge.log = lambda msg: None
    bridge._clock = lambda: clock
    bridge.tick_at = lambda t: _advance(bridge, t)
    return bridge, commands


def _advance(bridge: MarginBridge, t: float) -> None:
    bridge._clock = lambda: t
    bridge._tick()


def _strength_by_channel(commands: FakeCommands) -> dict[str, int]:
    out: dict[str, int] = {}
    for ch, value, _sid in commands.strength_calls:
        out[ch] = value
    return out


class GuardSensorTests(unittest.TestCase):

    def _guard(self, **overrides) -> EdgeGuard:
        merged = dict(_FAST_JUDGE)
        merged.update(overrides)
        return EdgeGuard(MarginConfig(merged))

    def test_starts_stimulating_when_pressure_fresh(self):
        guard = self._guard()
        guard.step(5.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        self.assertEqual(guard.outputs(0.0), (0, 0))

    def test_stays_idle_without_fresh_pressure(self):
        guard = self._guard()
        guard.step(5.0, None, False, now=0.0)
        self.assertEqual(guard.phase, PHASE_IDLE)
        self.assertEqual(guard.outputs(0.0), (0, 0))

    def test_stays_idle_in_off_mode(self):
        guard = self._guard(mode="off")
        guard.step(50.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_IDLE)
        self.assertEqual(guard.outputs(0.0), (0, 0))

    def test_crossing_threshold_cools_and_counts(self):
        guard = self._guard(ramp_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        self.assertEqual(guard.outputs(0.0)[0], 60)
        guard.step(41.0, None, True, now=1.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertEqual(guard.cycles, 1)
        self.assertEqual(guard.outputs(2.5), (0, 0))


    def test_edge_hold_requires_sustained_above(self):
        guard = self._guard(edge_hold_s=2.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(20.0, None, True, now=1.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(20.0, None, True, now=2.5)
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(20.0, None, True, now=3.1)
        self.assertEqual(guard.phase, PHASE_COOL)

    def test_edge_hold_resets_when_below(self):
        guard = self._guard(edge_hold_s=2.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(20.0, None, True, now=1.0)
        guard.step(10.0, None, True, now=2.0)
        guard.step(20.0, None, True, now=3.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(20.0, None, True, now=5.1)
        self.assertEqual(guard.phase, PHASE_COOL)

    def test_recovery_hold_requires_sustained_below(self):
        guard = self._guard(recovery_hold_s=3.0, cooldown_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        guard.step(5.0, None, True, now=2.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        guard.step(5.0, None, True, now=4.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        guard.step(5.0, None, True, now=5.1)
        self.assertEqual(guard.phase, PHASE_STIM)


    def test_jump_rate_triggers_edge_below_threshold(self):
        guard = self._guard(jump_rise=5.0, jump_window_s=1.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(10.0, None, True, now=0.5)
        guard.step(10.0, None, True, now=1.0)
        guard.step(16.0, None, True, now=1.5)
        self.assertEqual(guard.phase, PHASE_COOL)

    def test_jump_rate_below_threshold_no_trigger(self):
        guard = self._guard(jump_rise=5.0, jump_window_s=1.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(10.0, None, True, now=0.5)
        guard.step(13.0, None, True, now=1.5)
        self.assertEqual(guard.phase, PHASE_STIM)


    def test_punish_window_outputs_then_expires(self):
        guard = self._guard(ramp_s=0.0, punish_strength=120, punish_s=1.5)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertEqual(guard.outputs(1.5), (0, 120))
        self.assertEqual(guard.outputs(2.6), (0, 0))
        guard.step(50.0, None, True, now=3.0)
        self.assertEqual(guard.outputs(3.0), (0, 0))

    def test_punish_disabled_by_strength_zero_or_duration_zero(self):
        guard = self._guard(ramp_s=0.0, punish_strength=0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertEqual(guard.outputs(1.0), (0, 0))
        guard2 = self._guard(ramp_s=0.0, punish_s=0.0)
        guard2.step(10.0, None, True, now=0.0)
        guard2.step(45.0, None, True, now=1.0)
        self.assertEqual(guard2.outputs(1.0), (0, 0))

    def test_cool_strength_held_during_cooldown(self):
        guard = self._guard(ramp_s=0.0, cool_strength=20, punish_strength=0,
                            cooldown_s=10.0, recovery_threshold=20.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(42.0, None, True, now=5.0)
        self.assertEqual(guard.outputs(6.0), (20, 0))

    def test_recovery_requires_cooldown_and_low_pressure(self):
        guard = self._guard(cooldown_s=10.0, recovery_threshold=20.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(42.0, None, True, now=5.0)
        guard.step(5.0, None, True, now=12.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        guard.step(30.0, None, True, now=20.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        guard.step(5.0, None, True, now=25.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        self.assertEqual(guard.cycles, 1)

    def test_stale_pressure_failsafe_zeroes(self):
        guard = self._guard(ramp_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        self.assertEqual(guard.outputs(0.0)[0], 60)
        guard.step(None, None, False, now=99.0)
        self.assertEqual(guard.phase, PHASE_IDLE)
        self.assertEqual(guard.outputs(99.0), (0, 0))

    def test_offline_then_online_reenters_stim(self):
        guard = self._guard()
        guard.step(10.0, None, True, now=0.0)
        guard.step(None, None, False, now=1.0)
        self.assertEqual(guard.phase, PHASE_IDLE)
        guard.step(10.0, None, True, now=2.0)
        self.assertEqual(guard.phase, PHASE_STIM)

    def test_ramp_climbs_to_target(self):
        guard = self._guard(ramp_s=10.0, stim_strength=100)
        guard.step(10.0, None, True, now=0.0)
        self.assertEqual(guard.outputs(0.0)[0], 0)
        self.assertEqual(guard.outputs(5.0)[0], 50)
        self.assertEqual(guard.outputs(12.0)[0], 100)


    def test_adaptive_red_drops_after_edge_and_blue_follows(self):
        guard = self._guard(adapt_drop_pct=10.0, adapt_blue_follow=30.0)
        guard.step(10.0, None, True, now=0.0)
        self.assertAlmostEqual(guard.red_threshold(), 17.0)
        self.assertAlmostEqual(guard.blue_threshold(), 15.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertAlmostEqual(guard.red_threshold(), 15.5)
        self.assertAlmostEqual(guard.blue_threshold(), 14.55)

    def test_adaptive_red_timed_drop_when_no_edge(self):
        guard = self._guard(adapt_drop_delay_s=10.0, adapt_drop_rate=10.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(10.0, None, True, now=5.0)
        self.assertAlmostEqual(guard.red_threshold(), 17.0)
        guard.step(10.0, None, True, now=15.0)
        self.assertAlmostEqual(guard.red_threshold(), 15.5)

    def test_adaptive_blue_rises_when_cool_stuck(self):
        guard = self._guard(adapt_blue_delay_s=10.0,
                            adapt_blue_rise_rate=10.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        guard.step(15.5, None, True, now=5.0)
        self.assertAlmostEqual(guard.blue_threshold(), 15.0)
        guard.step(15.5, None, True, now=20.0)
        self.assertAlmostEqual(guard.blue_threshold(), 16.5)
        self.assertEqual(guard.phase, PHASE_STIM)

    def test_adaptive_disabled_by_toggles(self):
        guard = self._guard(adapt_stim=False, adapt_drop_pct=20.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertAlmostEqual(guard.red_threshold(), 17.0)
        guard2 = self._guard(adapt_cool=False, adapt_blue_delay_s=0.0,
                             adapt_blue_rise_rate=100.0)
        guard2.step(10.0, None, True, now=0.0)
        guard2.step(45.0, None, True, now=1.0)
        guard2.step(45.0, None, True, now=3.0)
        self.assertAlmostEqual(guard2.blue_threshold(), 15.0)


    def test_cycle_limit_release_at_next_edge(self):
        guard = self._guard(ramp_s=0.0, cooldown_s=5.0,
                            recovery_threshold=20.0, cycle_limit=2,
                            stim_strength=60, assist_strength=80,
                            release_s=15.0, punish_strength=100,
                            punish_s=1.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertEqual((guard.phase, guard.cycles), (PHASE_COOL, 1))
        self.assertGreater(guard.punish_until, 0.0)
        guard.step(5.0, None, True, now=10.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(45.0, None, True, now=20.0)
        self.assertEqual((guard.phase, guard.cycles), (PHASE_COOL, 2))
        guard.step(5.0, None, True, now=30.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(45.0, None, True, now=40.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        self.assertEqual(guard.cycles, 0)
        self.assertEqual(guard.outputs(40.0), (80, 0))
        guard.step(5.0, None, True, now=54.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        guard.step(5.0, None, True, now=56.0)
        self.assertEqual((guard.phase, guard.cycles), (PHASE_STIM, 0))

    def test_time_release_allowed_at_next_edge(self):
        guard = self._guard(time_release_s=30.0, assist_strength=80,
                            punish_strength=100, cooldown_s=5.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=10.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertGreater(guard.punish_until, 0.0)
        guard.step(5.0, None, True, now=20.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(45.0, None, True, now=31.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        self.assertEqual(guard.cycles, 0)
        self.assertEqual(guard.outputs(31.0), (80, 0))

    def test_release_holds_indefinitely_when_release_s_zero(self):
        guard = self._guard(cooldown_s=0.0, recovery_threshold=20.0,
                            cycle_limit=1, release_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        guard.step(5.0, None, True, now=2.0)
        guard.step(45.0, None, True, now=3.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        guard.step(45.0, None, True, now=999.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)

    def test_release_exits_via_failover_only(self):
        guard = self._guard(cooldown_s=0.0, recovery_threshold=20.0,
                            cycle_limit=1, release_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        guard.step(5.0, None, True, now=2.0)
        guard.step(45.0, None, True, now=3.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        guard.step(None, None, False, now=50.0)
        self.assertEqual(guard.phase, PHASE_IDLE)


class GuardAppTests(unittest.TestCase):

    def _guard(self, **overrides) -> EdgeGuard:
        merged = dict(_FAST_JUDGE)
        merged.update(overrides)
        return EdgeGuard(MarginConfig({"mode": "app", **merged}))

    def test_state_map(self):
        guard = self._guard(ramp_s=0.0, stim_strength=60,
                            assist_strength=80)
        guard.step(10.0, 1, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        self.assertEqual(guard.outputs(0.0), (60, 0))
        guard.step(10.0, 2, True, now=1.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertEqual(guard.cycles, 1)
        guard.step(10.0, 3, True, now=2.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertEqual(guard.cycles, 1)
        guard.step(10.0, 4, True, now=3.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        self.assertEqual(guard.outputs(3.0), (80, 0))
        guard.step(10.0, 1, True, now=4.0)
        self.assertEqual((guard.phase, guard.cycles), (PHASE_STIM, 0))

    def test_missing_session_stays_idle(self):
        guard = self._guard()
        guard.step(10.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_IDLE)

    def test_ramp_applies_in_app_mode(self):
        guard = self._guard(ramp_s=10.0, stim_strength=100)
        guard.step(10.0, 1, True, now=0.0)
        self.assertEqual(guard.outputs(0.0)[0], 0)
        self.assertEqual(guard.outputs(10.0)[0], 100)

    def test_release_conditions_not_applied_in_app_mode(self):
        guard = self._guard(cooldown_s=0.0, recovery_threshold=20.0,
                            cycle_limit=1, release_s=15.0,
                            time_release_s=30.0)
        guard.step(10.0, 1, True, now=0.0)
        guard.step(10.0, 2, True, now=1.0)
        guard.step(10.0, 1, True, now=40.0)
        self.assertEqual(guard.phase, PHASE_STIM)


class BridgeTickTests(unittest.IsolatedAsyncioTestCase):
    def test_tick_feeds_variables_and_drives_strength(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 10.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        self.assertEqual(commands.zap_calls, [])
        self.assertEqual(commands.fire_calls, [])
        self.assertEqual(commands.reset_calls, [])


    def test_red_timed_decay_is_linear_and_floored(self):
        bridge, commands = _bridge({"adapt_drop_delay_s": 10.0,
                                    "adapt_drop_rate": 10.0,
                                    "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        t = 100.0
        for _ in range(6000):
            t += 0.1
            bridge.tick_at(t)
        red = bridge.guard.red_threshold()
        self.assertAlmostEqual(red, 15.5)
        self.assertGreaterEqual(red, bridge.guard.blue_threshold() + 0.5)

    def test_red_edge_drop_respects_floor(self):
        bridge, commands = _bridge({"adapt_drop_pct": 90.0,
                                    "adapt_blue_follow": 0.0, "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)
        self.assertAlmostEqual(bridge.guard.red_threshold(), 15.5)
        self.assertGreaterEqual(bridge.guard.red_threshold(),
                                bridge.guard.blue_threshold() + 0.5)


    def test_outputs_table_ingests_pressure(self):
        commands = _commands(pressure=10.0)
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0}, commands)
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 10.0)
        self.assertEqual(bridge.engine.mappings.get("pressure"), None)
        self.assertIn("pressure", bridge.engine.out_values)
        self.assertIn("edge", bridge.engine.out_values)
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)
        self.assertEqual(bridge.guard.phase, PHASE_COOL)

    def test_outputs_row_redirects_second_bmtr(self):
        commands = _commands(pressure=10.0)
        commands.state.slots["s_bmt2"] = Slot(slot_id="s_bmt2", name="灵猫2",
                                              type="BMTR_1")
        commands.state.slots["s_bmt2"].pressure = 33.0
        bridge, commands = _bridge(
            {"ramp_s": 0.0, "smooth": 0.0,
             "outputs": [{"param": "BMTR.2.Pressure", "name": "pressure",
                          "expr": "{BMTR.2.Pressure}", "type": "Float"}]},
            commands)
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 33.0)

    def test_frozen_pressure_failsafe(self):
        commands = _commands(pressure=10.0)
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "sensor_timeout_s": 1.0}, commands)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.guard.phase, PHASE_STIM)
        for i in range(30):
            bridge.tick_at(101.0 + i * 0.1)
        self.assertEqual(bridge.guard.phase, PHASE_IDLE)
        commands.state.slots["s_bmt"].pressure = 10.5
        bridge.tick_at(105.0)
        self.assertEqual(bridge.guard.phase, PHASE_STIM)

    def test_user_config_chain_events_temps(self):
        commands = _commands(pressure=10.0)
        commands.state.slots["s_ovc"] = Slot(slot_id="s_ovc", name="负鼠",
                                             type="OVC_1")
        bridge, commands = _bridge(
            {"ramp_s": 0.0, "smooth": 0.0,
             "punish_strength": 100, "stim_strength": 60,
             "events": [{"name": "帧事件流", "trigger": "period", "arg": 100,
                         "actions": [
                             {"dir": "in", "param": "in_strength_a",
                              "var": "punish"},
                             {"dir": "in", "param": "in_ovc_strength_a",
                              "var": "stim"},
                         ]}],
             "temps": [{"name": "punish", "expr": "{punish_strength}"},
                       {"name": "stim", "expr": "{stim_strength}"}]},
            commands)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "{punish}")
        self.assertEqual(bridge.engine.mappings.get("in_ovc_strength_a"),
                         "{stim}")
        self.assertNotIn("in_strength_b", bridge.engine.mappings)
        by_target = {(ch, sid): v for ch, v, sid in commands.strength_calls}
        self.assertEqual(by_target[("A", "s_ovc")], 60)
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)
        by_target = {(ch, sid): v for ch, v, sid in commands.strength_calls}
        self.assertEqual(by_target[("A", "s_out")], 100)
        self.assertEqual(by_target[("A", "s_ovc")], 0)
        self.assertNotIn(("B", "s_out"), by_target)
        self.assertNotIn(("B", "s_ovc"), by_target)

    def test_user_mappings_override_events_chain(self):
        bridge, _ = _bridge(
            {"mappings": [{"param": "in_strength_a", "expr": "{on_edge}"}],
             "events": [{"actions": [{"dir": "in",
                                      "param": "in_strength_b",
                                      "var": "punish"}]}]})
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "{on_edge}")
        self.assertNotIn("in_strength_b", bridge.engine.mappings)

    def test_zero_noise_no_idle_flap(self):
        commands = _commands(pressure=0.0)
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0},
                                   commands=_commands(pressure=0.0))
        bridge.guard  # noqa
        phases = []
        t = 100.0
        for i in range(20):
            t += 0.1
            commands.state.slots["s_bmt"].pressure = 0.03 if i % 2 else 0.0
            bridge.tick_at(t)
            phases.append(bridge.guard.phase)
        self.assertNotIn(PHASE_STIM, phases)

    def test_push_chain_rows_push_core_params(self):
        commands = _commands(pressure=10.0)
        bridge, commands = _bridge({"ramp_s": 0.0}, commands)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "{stim_strength}")
        self.assertEqual(bridge.engine.mappings.get("in_strength_b"),
                         "{stim_strength}")
        dispatched = {(ch, sid) for ch, _v, sid in commands.strength_calls}
        self.assertIn(("A", "s_out"), dispatched)
        self.assertIn(("B", "s_out"), dispatched)

    def test_seven_variables_present(self):
        bridge, _ = _bridge({}, commands=_commands(pressure=30.0))
        bridge.tick_at(100.0)
        self.assertEqual(set(bridge.engine.signals), set(PARAM_DEFS))
        self.assertEqual(set(bridge.engine.signals),
                         {"pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "on_release",
                          "cycles"})
        self.assertEqual(bridge.engine.signals["cycles"], 0)
        self.assertEqual(bridge.engine.signals["on_release"], 0)

    def test_on_edge_flag(self):
        bridge, _ = _bridge({"edge_threshold": 40.0},
                            commands=_commands(pressure=30.0))
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)
        bridge2, _ = _bridge({"edge_threshold": 25.0},
                             commands=_commands(pressure=30.0))
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)

    def test_leak_compensation_offsets_pressure(self):
        bridge, _ = _bridge({"leak_comp": 3.0, "smooth": 0.0,
                             "edge_threshold": 17.0},
                            commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 13.0)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)
        bridge2, _ = _bridge({"leak_comp": 3.0, "smooth": 0.0},
                             commands=_commands(pressure=15.0))
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)

    def test_on_release_flag_follows_phase(self):
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "cooldown_s": 0.0,
                                    "recovery_threshold": 20.0,
                                    "cycle_limit": 1, "release_s": 0.0,
                                    "assist_strength": 90,
                                    "punish_strength": 120},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["on_release"], 0)
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)
        self.assertEqual(bridge.engine.signals["on_release"], 0)
        commands.state.slots["s_bmt"].pressure = 5.0
        bridge.tick_at(120.0)
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(130.0)
        self.assertEqual(bridge.engine.signals["on_release"], 1)
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        self.assertEqual(_strength_by_channel(commands), {"A": 90, "B": 90})

    def test_crossing_drives_punish_through_mapping(self):
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "punish_strength": 120,
                                    "punish_s": 1.0,
                                    "temps": [{"name": "out",
                                               "expr": "max({stim_strength},"
                                                       " {punish_strength})"}],
                                    "events": [
                   {"name": "推送", "trigger": "period", "arg": 100,
                    "actions": [
                        {"dir": "in", "param": "in_strength_a", "var": "out"},
                        {"dir": "in", "param": "in_strength_b", "var": "out"}]}]},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)
        self.assertEqual(_strength_by_channel(commands),
                         {"A": 120, "B": 120})
        bridge.tick_at(112.0)
        self.assertEqual(_strength_by_channel(commands), {"A": 0, "B": 0})

    def test_smooth_averages_pressure(self):
        bridge, commands = _bridge({"smooth": 0.5},
                                   commands=_commands(pressure=0.0))
        bridge.tick_at(100.0)
        commands.state.slots["s_bmt"].pressure = 30.0
        bridge.tick_at(110.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 15.0)
        bridge.tick_at(120.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 22.5)

    def test_no_sensor_stays_idle(self):
        bridge, commands = _bridge({"ramp_s": 0.0})
        del commands.state.slots["s_bmt"]
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)
        self.assertEqual(commands.strength_calls, [])

    def test_pause_zeroes_outputs_via_mapping(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        self.assertTrue(bridge.toggle_pause())
        bridge.tick_at(110.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)
        self.assertEqual(_strength_by_channel(commands), {"A": 0, "B": 0})
        self.assertFalse(bridge.toggle_pause())

    def test_no_direct_device_calls_ever(self):
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "punish_strength": 100,
                                    "cooldown_s": 0.0,
                                    "recovery_threshold": 20.0,
                                    "cycle_limit": 1, "release_s": 1.0},
                                   commands=_commands(pressure=10.0))
        for t, p in ((100, 10.0), (110, 45.0), (120, 5.0), (130, 45.0),
                     (140, 5.0), (150, 5.0), (160, 5.0)):
            commands.state.slots["s_bmt"].pressure = p
            bridge.tick_at(t)
        bridge.toggle_pause()
        bridge.tick_at(170)
        self.assertEqual(commands.zap_calls, [])
        self.assertEqual(commands.fire_calls, [])
        self.assertEqual(commands.reset_calls, [])

    async def test_stop_zeroes_via_mapping_only(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        await bridge.start()
        bridge.tick_at(100.0)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        await bridge.stop()
        self.assertEqual(_strength_by_channel(commands), {"A": 0, "B": 0})
        self.assertEqual(commands.reset_calls, [])
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)

    async def test_stop_without_output_change_touches_nothing(self):
        bridge, commands = _bridge({"mode": "off"})
        await bridge.start()
        await asyncio.sleep(0)
        await bridge.stop()
        self.assertEqual(commands.strength_calls, [])
        self.assertEqual(commands.reset_calls, [])

    async def test_reload_config_hot_swaps_mappings(self):
        bridge, _ = _bridge()
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "{stim_strength}")
        bridge.config["mappings"] = [
            {"param": "in_strength_a", "expr": "{punish_strength}"},
        ]
        await bridge.reload_config()
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "{punish_strength}")
        self.assertNotIn("in_strength_b", bridge.engine.mappings)


class PluginContractTests(unittest.TestCase):
    def _plugin_source(self) -> str:
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "modules", "margin_control",
            "plugin.py")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_meta_is_literal_and_complete(self):
        tree = ast.parse(self._plugin_source())
        meta = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "META"
                    for t in node.targets):
                meta = ast.literal_eval(node.value)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["id"], "margin_control")
        self.assertEqual(meta["settings_key"], "margin_control")
        self.assertEqual(meta["version"], "0.11.0")
        self.assertEqual(set(meta["params"]), set(PARAM_DEFS))
        self.assertEqual(set(meta["params"]),
                         {"pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "on_release",
                          "cycles"})
        cfg = meta["config"]
        for key in ("mode", "smooth", "sensor_timeout_s",
                    "edge_threshold", "recovery_threshold", "edge_hold_s",
                    "jump_rise", "jump_window_s", "cooldown_s",
                    "recovery_hold_s",
                    "cycle_limit", "time_release_s", "release_s",
                    "stim_strength", "cool_strength", "assist_strength",
                    "ramp_s", "punish_strength", "punish_s",
                    "adapt_stim", "adapt_drop_pct", "adapt_drop_delay_s",
                    "adapt_drop_rate", "adapt_blue_follow", "adapt_cool",
                    "adapt_blue_delay_s", "adapt_blue_rise_rate",
                    "leak_comp",
                    "mappings"):
            self.assertIn(key, cfg)
        self.assertNotIn("phase", cfg)
        self.assertNotIn("pressure_pct", cfg)
        self.assertNotIn("deny_zap_s", cfg)
        self.assertNotIn("release_fire_s", cfg)
        self.assertEqual(cfg["mappings"].get("rows"), "in")
        self.assertEqual(cfg["mode"].get("choices"),
                         ["sensor", "app", "off"])
        self.assertEqual(cfg["edge_threshold"].get("group"), "judge")
        self.assertEqual(cfg["cycle_limit"].get("group"), "release")
        self.assertEqual(cfg["adapt_drop_pct"].get("group"), "adapt")
        self.assertEqual(meta["actions"],
                         ["margin_reset_pressure", "margin_guard_toggle"])

    def test_config_defaults_cover_all_declared_keys(self):
        module = MarginControlModule()
        spec = module.config_spec()
        self.assertEqual(set(spec), set(META["config"]))
        for key, item in MARGIN_CONFIG_DEFAULTS.items():
            self.assertIn(key, spec)

    def test_link_params_returns_seven_variables(self):
        module = MarginControlModule()
        params = module.link_params()
        self.assertEqual([name for name, _label in params],
                         ["pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "on_release",
                          "cycles"])

    def test_button_actions_registered(self):
        from plugins import ButtonAction

        module = MarginControlModule()
        actions = module.button_actions()
        self.assertEqual([a.key for a in actions],
                         ["margin_reset_pressure", "margin_guard_toggle"])
        self.assertTrue(all(isinstance(a, ButtonAction) for a in actions))
        self.assertTrue(all(callable(a.on_press) for a in actions))

    def test_no_builtin_fallback_rows(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bare = MarginBridge(MarginConfig({}), commands.get_state, commands)
        bare.log = lambda msg: None
        self.assertEqual(bare.engine.mappings, {})
        self.assertEqual(bare.engine.outputs, [])
        bare._clock = lambda: 100.0
        bare._tick()
        self.assertEqual(bare.engine.out_values, {})
        self.assertEqual(commands.strength_calls, [])

    def test_module_class_attributes(self):
        module = MarginControlModule()
        self.assertEqual(module.id, "margin_control")
        self.assertEqual(module.settings_key, "margin_control")
        self.assertFalse(module.is_running())
        self.assertTrue(callable(module.config_spec))

    def test_button_press_without_start_is_safe(self):
        module = MarginControlModule()
        module.on_load(type("Ctx", (), {
            "log": staticmethod(lambda msg: None),
            "submit": staticmethod(lambda coro: None),
            "engine": None,
            "settings": {"outputs": []},
        })())
        module._press_guard_toggle(None, None)
        module.on_unload()


if __name__ == "__main__":
    unittest.main()
