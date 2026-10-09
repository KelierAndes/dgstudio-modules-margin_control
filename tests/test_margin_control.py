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

# 宿主 ModuleContext 已拦下的设备直写方法 + 直写设备固件的旁路命令：
# 模块一旦碰到就抛，守门用例据此证明闭环全程不碰设备
BLOCKED_METHODS = ("set_strength", "add_strength", "reset_strength", "set_wave",
                   "push_pulse_stream", "fire", "fire_start", "fire_stop",
                   "zap", "set_intensity_param", "reset_pressure",
                   "emergency_stop")


class DeviceTamper(Exception):
    """设备被模块碰到的信号。"""


class NeverCommands:
    """替代宿主引擎：任何设备动作都立刻抛错，同时提供只读的 state。"""

    def __init__(self, pressure: float | None = None,
                 edge: int | None = None):
        self.state = EngineState(backend="ble", connected=True, paired=True)
        self.state.slots["s_out"] = Slot(slot_id="s_out", name="郊狼",
                                         type="COYOTE_030")
        self.state.slots["s_bmt"] = Slot(slot_id="s_bmt", name="灵猫",
                                         type="BMTR_010")
        if pressure is not None:
            self.state.slots["s_bmt"].pressure = pressure
        if edge is not None:
            self.state.slots["s_bmt"].edge_state = edge
        self.touched: list[str] = []

    def get_state(self):
        return self.state

    def wave_selection(self):
        return {"A": SILENT, "B": SILENT}


def _tamper(name: str):
    def _raise(self, *args, **kwargs):
        self.touched.append(name)
        raise DeviceTamper(f"模块直写设备被调用：{name}()")
    return _raise


for _name in BLOCKED_METHODS:
    setattr(NeverCommands, _name, _tamper(_name))


class _Ingest:
    """模拟宿主事件流的写入卡片：把核心读数写进模块的共享变量。

    v0.12 起闭环的输入不再由模块自己读设备，而是事件流把 ``BMTR.Pressure`` /
    ``BMTR.EdgeState`` 写进 ``pressure`` / ``edge`` 两个变量；单测里用这个对象
    代替画布，每拍 ``_tick()`` 前把当前读数落到变量表上。
    """

    def __init__(self, bridge: MarginBridge, pressure=None, edge=None):
        self.bridge = bridge
        self.pressure = pressure
        self.edge = edge

    def write(self) -> None:
        if self.pressure is not None:
            self.bridge.engine.temps["pressure"] = float(self.pressure)
        if self.edge is not None:
            self.bridge.engine.temps["edge"] = int(self.edge)


def _bridge(config: dict | None = None, commands: NeverCommands | None = None,
            clock: float = 100.0, pressure=None,
            edge=None) -> tuple[MarginBridge, NeverCommands, _Ingest]:
    commands = commands or NeverCommands(pressure=pressure, edge=edge)
    merged = dict(_FAST_JUDGE)
    merged.update(config or {})
    bridge = MarginBridge(MarginConfig(merged), commands.get_state, commands)
    bridge.log = lambda msg: None
    bridge._clock = lambda: clock
    bridge.ingest = _Ingest(bridge, pressure, edge)
    bridge.tick_at = lambda t: _advance(bridge, t)
    return bridge, commands, bridge.ingest


def _advance(bridge: MarginBridge, t: float) -> None:
    bridge._clock = lambda: t
    bridge.ingest.write()
    bridge._tick()


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
        guard = self._guard(adapt_stim=True, adapt_drop_pct=10.0,
                            adapt_blue_follow=30.0)
        guard.step(10.0, None, True, now=0.0)
        self.assertAlmostEqual(guard.red_threshold(), 17.0)
        self.assertAlmostEqual(guard.blue_threshold(), 15.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertAlmostEqual(guard.red_threshold(), 15.5)
        self.assertAlmostEqual(guard.blue_threshold(), 14.55)

    def test_adaptive_red_timed_drop_when_no_edge(self):
        guard = self._guard(adapt_stim=True, adapt_drop_delay_s=10.0,
                            adapt_drop_rate=10.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(10.0, None, True, now=5.0)
        self.assertAlmostEqual(guard.red_threshold(), 17.0)
        guard.step(10.0, None, True, now=15.0)
        self.assertAlmostEqual(guard.red_threshold(), 15.5)

    def test_adaptive_blue_rises_when_cool_stuck(self):
        guard = self._guard(adapt_cool=True, adapt_blue_delay_s=10.0,
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

    def test_adaptation_switches_default_off(self):
        guard = self._guard(adapt_drop_pct=10.0, adapt_drop_delay_s=0.0,
                            adapt_drop_rate=10.0, adapt_blue_delay_s=0.0,
                            adapt_blue_rise_rate=100.0)
        self.assertFalse(guard._b("adapt_stim"))
        self.assertFalse(guard._b("adapt_cool"))
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertAlmostEqual(guard.red_threshold(), 17.0)
        self.assertAlmostEqual(guard.blue_threshold(), 15.0)
        guard.step(15.5, None, True, now=20.0)
        self.assertAlmostEqual(guard.blue_threshold(), 15.0)

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

    def test_tick_ingests_variables_and_publishes_strength(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0}, pressure=10.0)
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.temps["pressure"], 10.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 10.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        self.assertEqual(bridge.engine.mappings, {})
        self.assertEqual(bridge.engine.outputs, [])
        self.assertEqual(bridge.engine.out_values, {})
        self.assertEqual(commands.touched, [])

    def test_crossing_edge_publishes_punish_then_zeroes(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                            "punish_strength": 120,
                                            "punish_s": 1.0,
                                            "cooldown_s": 0.0,
                                            "recovery_threshold": 20.0},
                                           pressure=10.0)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)
        ingest.pressure = 45.0
        bridge.tick_at(110.0)
        self.assertEqual(bridge.guard.phase, PHASE_COOL)
        self.assertEqual(bridge.engine.signals["punish_strength"], 120)
        self.assertEqual(bridge.engine.signals["cycles"], 1)
        bridge.tick_at(112.0)
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        self.assertEqual(commands.touched, [])

    def test_release_phase_publishes_assist_strength(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                            "cooldown_s": 0.0,
                                            "recovery_threshold": 20.0,
                                            "cycle_limit": 1,
                                            "release_s": 0.0,
                                            "assist_strength": 90,
                                            "punish_strength": 120},
                                           pressure=10.0)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["on_release"], 0)
        ingest.pressure = 45.0
        bridge.tick_at(110.0)
        ingest.pressure = 5.0
        bridge.tick_at(120.0)
        ingest.pressure = 45.0
        bridge.tick_at(130.0)
        self.assertEqual(bridge.engine.signals["on_release"], 1)
        self.assertEqual(bridge.engine.signals["stim_strength"], 90)
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        self.assertEqual(commands.touched, [])

    def test_red_timed_decay_is_linear_and_floored(self):
        bridge, commands, ingest = _bridge({"adapt_stim": True,
                                            "adapt_drop_delay_s": 10.0,
                                            "adapt_drop_rate": 10.0,
                                            "smooth": 0.0},
                                           pressure=10.0)
        bridge.tick_at(100.0)
        t = 100.0
        for _ in range(6000):
            t += 0.1
            bridge.tick_at(t)
        red = bridge.guard.red_threshold()
        self.assertAlmostEqual(red, 15.5)
        self.assertGreaterEqual(red, bridge.guard.blue_threshold() + 0.5)

    def test_red_edge_drop_respects_floor(self):
        bridge, commands, ingest = _bridge({"adapt_stim": True,
                                            "adapt_drop_pct": 90.0,
                                            "adapt_blue_follow": 0.0,
                                            "smooth": 0.0}, pressure=10.0)
        bridge.tick_at(100.0)
        ingest.pressure = 45.0
        bridge.tick_at(110.0)
        self.assertAlmostEqual(bridge.guard.red_threshold(), 15.5)
        self.assertGreaterEqual(bridge.guard.red_threshold(),
                                bridge.guard.blue_threshold() + 0.5)

    def test_app_mode_ingests_edge_variable(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0, "mode": "app",
                                            "smooth": 0.0},
                                           pressure=10.0, edge=1)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["edge"], 1)
        self.assertEqual(bridge.guard.phase, PHASE_STIM)
        ingest.edge = 4
        bridge.tick_at(110.0)
        self.assertEqual(bridge.guard.phase, PHASE_RELEASE)
        self.assertEqual(commands.touched, [])

    def test_host_attach_temps_shares_variable_space(self):
        shared = {"punish": 5.0}
        bridge, commands, ingest = _bridge(
            {"ramp_s": 0.0, "smooth": 0.0,
             "temps": [{"name": "seed", "value": 7.0}]}, pressure=10.0)
        bridge.engine.attach_temps(shared)
        bridge.tick_at(100.0)
        self.assertIs(bridge.engine.temps, shared)
        self.assertAlmostEqual(shared["pressure"], 10.0)
        self.assertAlmostEqual(shared["punish"], 5.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)
        self.assertEqual(commands.touched, [])

    def test_frozen_pressure_failsafe(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                            "sensor_timeout_s": 1.0},
                                           pressure=10.0)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.guard.phase, PHASE_STIM)
        for i in range(30):
            bridge.tick_at(101.0 + i * 0.1)
        self.assertEqual(bridge.guard.phase, PHASE_IDLE)
        ingest.pressure = 10.5
        bridge.tick_at(105.0)
        self.assertEqual(bridge.guard.phase, PHASE_STIM)

    def test_zero_noise_no_idle_flap(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0, "smooth": 0.0},
                                           pressure=0.0)
        phases = []
        t = 100.0
        for i in range(20):
            t += 0.1
            ingest.pressure = 0.03 if i % 2 else 0.0
            bridge.tick_at(t)
            phases.append(bridge.guard.phase)
        self.assertNotIn(PHASE_STIM, phases)

    def test_seven_variables_present(self):
        bridge, commands, ingest = _bridge({}, pressure=30.0)
        bridge.tick_at(100.0)
        self.assertEqual(set(bridge.engine.signals), set(PARAM_DEFS))
        self.assertEqual(set(bridge.engine.signals),
                         {"pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "on_release",
                          "cycles"})
        self.assertEqual(bridge.engine.signals["cycles"], 0)
        self.assertEqual(bridge.engine.signals["on_release"], 0)

    def test_on_edge_flag(self):
        bridge, commands, ingest = _bridge({"edge_threshold": 40.0},
                                           pressure=30.0)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)
        bridge2, commands2, ingest2 = _bridge({"edge_threshold": 25.0},
                                              pressure=30.0)
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)

    def test_leak_compensation_offsets_pressure(self):
        bridge, commands, ingest = _bridge({"leak_comp": 3.0, "smooth": 0.0,
                                            "edge_threshold": 17.0},
                                           pressure=10.0)
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 13.0)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)
        bridge2, commands2, ingest2 = _bridge({"leak_comp": 3.0, "smooth": 0.0},
                                              pressure=15.0)
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)

    def test_smooth_averages_pressure(self):
        bridge, commands, ingest = _bridge({"smooth": 0.5}, pressure=0.0)
        bridge.tick_at(100.0)
        ingest.pressure = 30.0
        bridge.tick_at(110.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 15.0)
        bridge.tick_at(120.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 22.5)

    def test_no_sensor_stays_idle(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0})
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)
        self.assertEqual(bridge.engine.signals["pressure"], 0)

    def test_values_prefer_published_readings(self):
        """事件流取值口径：设备读数 < 共享变量 < 模块发布的读数。"""
        bridge, commands, ingest = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                            "leak_comp": 3.0}, pressure=10.0)
        bridge.engine.temps["my_var"] = 42.0
        bridge.tick_at(100.0)
        vals = bridge.engine.values()
        self.assertAlmostEqual(vals["BMTR.Pressure"], 10.0)
        self.assertEqual(vals["my_var"], 42.0)
        self.assertAlmostEqual(vals["pressure"], 13.0)

    def test_pause_zeroes_published_strength(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0}, pressure=10.0)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)
        self.assertTrue(bridge.toggle_pause())
        bridge.tick_at(110.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        self.assertEqual(commands.touched, [])
        self.assertFalse(bridge.toggle_pause())

    async def test_stop_zeroes_published_strength(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0}, pressure=10.0)
        await bridge.start()
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)
        await bridge.stop()
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        self.assertEqual(commands.touched, [])

    async def test_stop_without_output_change_touches_nothing(self):
        bridge, commands, ingest = _bridge({"mode": "off"})
        await bridge.start()
        await asyncio.sleep(0)
        await bridge.stop()
        self.assertEqual(commands.touched, [])

    async def test_reload_config_does_not_touch_device(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0}, pressure=10.0)
        bridge.tick_at(100.0)
        bridge.config["stim_strength"] = 120
        await bridge.reload_config()
        bridge.tick_at(110.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 120)
        self.assertEqual(commands.touched, [])

    def test_board_keeps_host_required_members(self):
        bridge, commands, ingest = _bridge({}, pressure=10.0)
        self.assertIsInstance(bridge.engine.signals, dict)
        self.assertIsInstance(bridge.engine.errors, dict)
        self.assertIsInstance(bridge.engine.temps, dict)
        bridge.engine.pump()
        bridge.engine.signal("pressure", 12.5)
        self.assertEqual(bridge.engine.signals["pressure"], 12.5)
        bridge.engine.reset()
        self.assertEqual(bridge.engine.signals, {})


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
        self.assertEqual(meta["version"], "0.13.0")
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
                    "leak_comp"):
            self.assertIn(key, cfg)
        self.assertNotIn("mappings", cfg)
        self.assertNotIn("outputs", cfg)
        self.assertNotIn("reads", meta)
        self.assertEqual([t["key"] for t in meta["temps"]],
                         ["pressure", "edge"])
        self.assertNotIn("phase", cfg)
        self.assertNotIn("pressure_pct", cfg)
        self.assertNotIn("deny_zap_s", cfg)
        self.assertNotIn("release_fire_s", cfg)
        self.assertEqual(cfg["mode"].get("choices"),
                         ["sensor", "app", "off"])
        self.assertEqual(cfg["edge_threshold"].get("group"), "judge")
        self.assertEqual(cfg["cycle_limit"].get("group"), "release")
        self.assertEqual(cfg["adapt_drop_pct"].get("group"), "adapt")
        self.assertIs(cfg["adapt_stim"].get("default"), False)
        self.assertIs(cfg["adapt_cool"].get("default"), False)
        self.assertIs(MARGIN_CONFIG_DEFAULTS["adapt_stim"], False)
        self.assertIs(MARGIN_CONFIG_DEFAULTS["adapt_cool"], False)
        self.assertEqual(meta["actions"], ["margin_guard_toggle"])

    def test_config_defaults_cover_all_declared_keys(self):
        module = MarginControlModule()
        spec = module.config_spec()
        self.assertEqual(set(spec), set(META["config"]))
        for key, item in MARGIN_CONFIG_DEFAULTS.items():
            self.assertIn(key, spec)

    def test_link_params_declare_direction_and_type(self):
        module = MarginControlModule()
        rows = module.link_params()
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        self.assertEqual([row["name"] for row in rows],
                         ["pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "on_release",
                          "cycles"])
        by = {row["name"]: row for row in rows}
        # 读数方向：in=宿主可读；pressure/edge 是闭环输入，宿主也要能写
        self.assertEqual({row["dir"] for name, row in by.items()
                          if name in ("pressure", "edge")}, {"inout"})
        self.assertEqual({row["dir"] for name, row in by.items()
                          if name not in ("pressure", "edge")}, {"in"})
        self.assertEqual(by["pressure"]["type"], "Float")
        self.assertEqual(by["on_edge"]["type"], "Bool")
        self.assertEqual(by["cycles"]["type"], "Int")

    def test_declared_temps_match_params_direction(self):
        """META["temps"] 的输入声明与 link_params 同向，登记合并后不会退化成只读。"""
        module = MarginControlModule()
        by = {row["name"]: row["dir"] for row in module.link_params()}
        for row in META["temps"]:
            self.assertEqual(row["dir"], by[row["key"]])

    def test_button_actions_registered(self):
        from plugins import ButtonAction

        module = MarginControlModule()
        actions = module.button_actions()
        self.assertEqual([a.key for a in actions], ["margin_guard_toggle"])
        self.assertTrue(all(isinstance(a, ButtonAction) for a in actions))
        self.assertTrue(all(callable(a.on_press) for a in actions))

    def test_no_builtin_dispatch_rows(self):
        bridge, commands, ingest = _bridge({"ramp_s": 0.0}, pressure=10.0)
        bare = MarginBridge(MarginConfig({}), commands.get_state, commands)
        bare.log = lambda msg: None
        self.assertEqual(bare.engine.mappings, {})
        self.assertEqual(bare.engine.outputs, [])
        self.assertEqual(bare.engine.out_values, {})
        bare._clock = lambda: 100.0
        bare.ingest = _Ingest(bare)
        bare._tick()
        self.assertEqual(bare.engine.temps, {})
        self.assertEqual(commands.touched, [])

    def test_module_class_attributes(self):
        module = MarginControlModule()
        self.assertEqual(module.id, "margin_control")
        self.assertEqual(module.settings_key, "margin_control")
        self.assertFalse(module.is_running())
        self.assertTrue(callable(module.config_spec))

    def test_button_press_without_start_is_safe(self):
        module = MarginControlModule()
        module.on_load(_Ctx(state=EngineState(backend="ble")))
        module._press_guard_toggle(None, None)
        module.on_unload()

    def test_legacy_dispatch_settings_dropped_with_one_log(self):
        from modules.margin_control.plugin import drop_mapping_tables

        settings = {"events": [{"name": "强度推送", "trigger": "period",
                                "arg": 50, "actions": []}],
                    "temps": [{"name": "strength_out",
                               "expr": "max({stim_strength},{punish_strength})"}],
                    "mappings": [], "outputs": [],
                    "edge_threshold": 17.0}
        logs: list[str] = []
        self.assertTrue(drop_mapping_tables(settings, logs.append))
        for key in ("events", "temps", "mappings", "outputs"):
            self.assertNotIn(key, settings)
        self.assertEqual(settings["edge_threshold"], 17.0)
        self.assertEqual(len(logs), 1)
        self.assertIn("事件流", logs[0])
        self.assertFalse(drop_mapping_tables(settings, logs.append))
        self.assertEqual(len(logs), 1)


class _Ctx:
    """宿主 ModuleContext 桩：十个被拦方法 + reset_pressure 一律抛错。"""

    def __init__(self, state: EngineState | None = None):
        self.state = state or EngineState(backend="ble", connected=True,
                                          paired=True)
        self.settings: dict = {}
        self.logs: list[str] = []
        self.events = _Events()
        self.engine = NeverCommands()
        self.touched: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def get_state(self):
        return self.state

    def submit(self, coro):
        coro.close()
        return None

    def set_temp(self, key, value) -> None:
        return None

    def resolve_slot(self, slot_id=None, family=None, output_only=False):
        return None


class _Events:
    def on(self, *args):
        pass

    def off(self, *args):
        pass

    def emit(self, event, *args):
        pass


def _ctx_guard(name: str):
    def _raise(self, *args, **kwargs):
        self.touched.append(name)
        raise DeviceTamper(f"模块直写设备被调用：{name}()")
    return _raise


for _name in BLOCKED_METHODS:
    setattr(_Ctx, _name, _ctx_guard(_name))


class GuardModuleCycleTests(unittest.IsolatedAsyncioTestCase):
    """守门用例：一个会对设备写入抛错的宿主桩要撑过模块的完整生命周期。"""

    async def test_full_module_cycle_never_touches_device(self):
        ctx = _Ctx()
        mod = MarginControlModule()
        mod.on_load(ctx)
        await mod.start()
        try:
            bridge = mod.bridge
            self.assertTrue(bridge.is_running() if hasattr(bridge, "is_running")
                            else bridge._running)
            bridge.ingest = _Ingest(bridge)
            bridge.tick_at = lambda t: _advance(bridge, t)
            for t, p in ((100.0, 10.0), (110.0, 45.0), (120.0, 5.0),
                         (130.0, 45.0), (140.0, 5.0), (150.0, 45.0)):
                bridge.ingest.pressure = p
                bridge.tick_at(t)
            bridge.toggle_pause()
            bridge.tick_at(160.0)
            bridge.toggle_pause()
            bridge.tick_at(170.0)
            await mod.reload_config()
            self.assertIn("stim_strength", bridge.engine.signals)
            self.assertEqual(ctx.touched, [])
            self.assertEqual(ctx.engine.touched, [])
            for row in mod.link_params():
                self.assertIn(row["dir"], ("in", "out", "inout"))
        finally:
            await mod.stop()
            mod.on_unload()
        self.assertEqual(ctx.touched, [])


if __name__ == "__main__":
    unittest.main()
