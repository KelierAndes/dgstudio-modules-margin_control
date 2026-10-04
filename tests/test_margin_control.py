"""灵猫边控联动模块单测。

覆盖：闭环状态机纯逻辑（sensor 气压闭环的到边/冷静/恢复、循环上限释放、
释放计时、app 跟随官方边控状态 0-4、爬升与失联 fail-safe、惩罚窗口）、
桥接器一拍推进（六个映射变量喂入 + 默认行刺激器/惩罚器最大值派发 + 停止
经映射表归零）、映射表默认行与热更新、**设备控制只经映射表**（不直接调用
zap/fire/reset）、插件 META / link_params / 按键动作契约。不依赖真实设备
（引擎命令层用假件记录派发）。

运行（模块仓库根目录）::

    python -m unittest discover -s tests
"""
from __future__ import annotations

import ast
import asyncio
import os
import unittest

import _bootstrap  # noqa: F401  定位核心仓库并挂 sys.path

from dglab.state import EngineState, Slot
from dglab.waves import SILENT

from modules.margin_control.bridge import (DEFAULT_MAPPINGS, PARAM_DEFS,
                                           EdgeGuard, MarginBridge,
                                           MarginConfig, PHASE_COOL,
                                           PHASE_IDLE, PHASE_RELEASE,
                                           PHASE_STIM)
from modules.margin_control.plugin import (MARGIN_CONFIG_DEFAULTS, META,
                                           MarginControlModule)


class FakeCommands:
    """引擎命令层假件：记录映射派发（不触碰真实设备）。

    同时用于断言「设备控制只经映射表」：模块运行期除映射派发器适配的
    set_strength / set_wave 外，不得出现 zap / fire / reset 等直呼。
    """

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

    # ---- 状态 ----

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

    # ---- 命令（同步记账 + 空协程） ----

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


def _bridge(config: dict | None = None, commands: FakeCommands | None = None,
            clock: float = 100.0) -> tuple[MarginBridge, FakeCommands]:
    """带假命令层的桥接器（时钟固定，测试里用 tick_at 手动推进）。"""
    commands = commands or _commands()
    bridge = MarginBridge(MarginConfig(config or {}), commands.get_state,
                          commands)
    bridge.log = lambda msg: None
    bridge._clock = lambda: clock
    bridge.tick_at = lambda t: _advance(bridge, t)
    return bridge, commands


def _advance(bridge: MarginBridge, t: float) -> None:
    """把时钟推进到 t 并跑一拍（映射派发同步记账，无真实协程）。"""
    bridge._clock = lambda: t
    bridge._tick()


def _strength_by_channel(commands: FakeCommands) -> dict[str, int]:
    """假件记录的各通道最新强度派发。"""
    out: dict[str, int] = {}
    for ch, value, _sid in commands.strength_calls:
        out[ch] = value
    return out


# ---------------------------------------------------------------- 状态机

class GuardSensorTests(unittest.TestCase):
    """sensor 模式：气压闭环（到边 → 冷静 → 恢复 / 释放）。"""

    def _guard(self, **overrides) -> EdgeGuard:
        return EdgeGuard(MarginConfig(overrides or {}))

    def test_starts_stimulating_when_pressure_fresh(self):
        guard = self._guard()
        guard.step(5.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        self.assertEqual(guard.outputs(0.0), (0, 0))      # 爬升起点

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
        self.assertEqual(guard.outputs(0.0)[0], 60)       # 刺激器强度
        # 气压越过边缘阈值 40 → 冷静 + 记一次循环 + 撤除刺激
        guard.step(41.0, None, True, now=1.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertEqual(guard.cycles, 1)
        self.assertEqual(guard.outputs(2.5), (0, 0))      # 惩罚窗口(1s)已过

    def test_punish_window_outputs_then_expires(self):
        guard = self._guard(ramp_s=0.0, punish_strength=120, punish_s=1.5)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)             # 到边进冷静
        self.assertEqual(guard.outputs(1.5), (0, 120))    # 窗口内惩罚输出
        self.assertEqual(guard.outputs(2.6), (0, 0))      # 窗口(1.5s)已过
        # 冷静期内重复评估不再重新开窗
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
        self.assertEqual(guard.outputs(6.0), (20, 0))     # 冷静期维持强度

    def test_recovery_requires_cooldown_and_low_pressure(self):
        guard = self._guard(ramp_s=0.0, cooldown_s=10.0,
                            recovery_threshold=20.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(42.0, None, True, now=5.0)
        # 未满冷静时长：即使气压已回落也不恢复
        guard.step(5.0, None, True, now=12.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        # 时长已满但气压仍高：继续冷静（回差防抖）
        guard.step(30.0, None, True, now=20.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        # 时长满 + 气压回落 → 恢复刺激
        guard.step(5.0, None, True, now=25.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        self.assertEqual(guard.cycles, 1)

    def test_stale_pressure_failsafe_zeroes(self):
        guard = self._guard(ramp_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        self.assertEqual(guard.outputs(0.0)[0], 60)
        guard.step(None, None, False, now=99.0)   # 气压失联
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
        self.assertEqual(guard.outputs(0.0)[0], 0)        # 爬升起点
        self.assertEqual(guard.outputs(5.0)[0], 50)       # 中点
        self.assertEqual(guard.outputs(12.0)[0], 100)     # 爬满

    def test_cycle_limit_triggers_release_then_restarts(self):
        guard = self._guard(ramp_s=0.0, cooldown_s=5.0,
                            recovery_threshold=20.0, cycle_limit=2,
                            stim_strength=60, assist_strength=80,
                            release_s=15.0)
        # 第 1 轮：到边 → 冷静 → 恢复（未达上限，回刺激）
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertEqual((guard.phase, guard.cycles), (PHASE_COOL, 1))
        guard.step(5.0, None, True, now=10.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        # 第 2 轮：到边 → 冷静 → 达上限，进释放
        guard.step(45.0, None, True, now=20.0)
        self.assertEqual((guard.phase, guard.cycles), (PHASE_COOL, 2))
        guard.step(5.0, None, True, now=30.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        # 助力强度经刺激器变量输出（不爬升）
        self.assertEqual(guard.outputs(30.0), (80, 0))
        # 释放计时（15s）满 → 循环计数清零、回到刺激
        guard.step(5.0, None, True, now=44.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        guard.step(5.0, None, True, now=46.0)
        self.assertEqual((guard.phase, guard.cycles), (PHASE_STIM, 0))

    def test_release_holds_indefinitely_when_release_s_zero(self):
        guard = self._guard(cooldown_s=0.0, recovery_threshold=20.0,
                            cycle_limit=1, release_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        guard.step(5.0, None, True, now=100.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        guard.step(5.0, None, True, now=999.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)      # 一直保持

    def test_release_exits_via_failover_only(self):
        """释放期内气压失联 → fail-safe 待机。"""
        guard = self._guard(cooldown_s=0.0, recovery_threshold=20.0,
                            cycle_limit=1, release_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        guard.step(5.0, None, True, now=2.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        guard.step(None, None, False, now=50.0)
        self.assertEqual(guard.phase, PHASE_IDLE)


class GuardAppTests(unittest.TestCase):
    """app 模式：跟随官方边控会话（edgeState 0-4）。"""

    def _guard(self, **overrides) -> EdgeGuard:
        return EdgeGuard(MarginConfig({"mode": "app", **overrides}))

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
        self.assertEqual(guard.cycles, 1)         # 判定不计新循环
        guard.step(10.0, 4, True, now=3.0)
        self.assertEqual(guard.phase, PHASE_RELEASE)
        self.assertEqual(guard.outputs(3.0), (80, 0))     # 助力经刺激器输出
        # 会话回到刺激（新一轮）→ 循环计数清零
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

    def test_cycle_limit_not_applied_in_app_mode(self):
        """app 模式循环保守计数但不强制释放——释放只由会话状态 4 决定。"""
        guard = self._guard(cooldown_s=0.0, recovery_threshold=20.0,
                            cycle_limit=1, release_s=15.0)
        guard.step(10.0, 1, True, now=0.0)
        guard.step(10.0, 2, True, now=1.0)        # cycles=1 已达上限
        guard.step(10.0, 1, True, now=2.0)
        self.assertEqual(guard.phase, PHASE_STIM)  # 仍回刺激，不进释放


# ---------------------------------------------------------------- 桥接器

class BridgeTickTests(unittest.TestCase):
    def test_tick_feeds_variables_and_drives_strength(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 10.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        # 默认行：刺激器/惩罚器最大值驱动 A/B 强度（全部经映射表派发）
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        self.assertEqual(commands.zap_calls, [])
        self.assertEqual(commands.fire_calls, [])
        self.assertEqual(commands.reset_calls, [])

    def test_six_variables_present(self):
        bridge, _ = _bridge({}, commands=_commands(pressure=30.0))
        bridge.tick_at(100.0)
        self.assertEqual(set(bridge.engine.signals), set(PARAM_DEFS))
        self.assertEqual(set(bridge.engine.signals),
                         {"pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "cycles"})
        self.assertEqual(bridge.engine.signals["cycles"], 0)

    def test_on_edge_flag(self):
        bridge, _ = _bridge({}, commands=_commands(pressure=30.0))
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)
        bridge2, _ = _bridge({"edge_threshold": 25.0},
                             commands=_commands(pressure=30.0))
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)

    def test_crossing_drives_punish_through_mapping(self):
        """到边 → 冷静 + 惩罚窗口：默认行派发惩罚器强度，窗口过后归零。"""
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "punish_strength": 120,
                                    "punish_s": 1.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        # 气压升到阈值之上 → 冷静 + 惩罚输出经映射行派发
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)
        self.assertEqual(_strength_by_channel(commands),
                         {"A": 120, "B": 120})
        # 惩罚窗口（1s）过后：冷静强度 0 → 映射行派发归零
        bridge.tick_at(112.0)
        self.assertEqual(_strength_by_channel(commands), {"A": 0, "B": 0})

    def test_mapping_dispatch_targets_family_first_device(self):
        """无绑定设置：映射派发按家族解析第一台输出设备。"""
        commands = _commands(pressure=10.0)
        commands.state.slots["s_out2"] = Slot(slot_id="s_out2", name="郊狼2",
                                              type="COYOTE_031")
        bridge, _ = _bridge({"ramp_s": 0.0}, commands)
        bridge.tick_at(100.0)
        self.assertTrue(all(sid == "s_out"          # 家族内排序第一台
                            for _ch, _v, sid in commands.strength_calls))

    def test_sensor_slot_binding(self):
        commands = _commands(pressure=10.0)
        commands.state.slots["s_bmt2"] = Slot(slot_id="s_bmt2", name="灵猫2",
                                              type="BMTR_020")
        commands.state.slots["s_bmt2"].pressure = 20.0
        bridge, _ = _bridge({"sensor_slot": "s_bmt2"}, commands)
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 20.0)

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

    def test_cycle_limit_release_drives_stimulator_via_mapping(self):
        """循环上限达成 → 释放期助力强度经刺激器变量映射派发。"""
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "cooldown_s": 0.0,
                                    "recovery_threshold": 20.0,
                                    "cycle_limit": 1,
                                    "assist_strength": 90,
                                    "release_s": 15.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)                     # 刺激 60
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)                     # 到边 → 冷静(cycles=1)
        commands.state.slots["s_bmt"].pressure = 5.0
        bridge.tick_at(120.0)                     # 冷静期满 → 释放
        self.assertEqual(bridge.engine.signals["cycles"], 1)
        self.assertEqual(_strength_by_channel(commands), {"A": 90, "B": 90})
        # 释放计时满 → 清零计数重新刺激（爬升从 0 开始）
        bridge2, commands2 = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                      "cooldown_s": 0.0,
                                      "recovery_threshold": 20.0,
                                      "cycle_limit": 1,
                                      "assist_strength": 90,
                                      "release_s": 15.0},
                                     commands=_commands(pressure=10.0))
        bridge2.tick_at(100.0)
        commands2.state.slots["s_bmt"].pressure = 45.0
        bridge2.tick_at(110.0)
        commands2.state.slots["s_bmt"].pressure = 5.0
        bridge2.tick_at(120.0)
        bridge2.tick_at(140.0)                    # 释放计时到
        self.assertEqual(bridge2.engine.signals["cycles"], 0)
        self.assertEqual(_strength_by_channel(commands2),
                         {"A": 60, "B": 60})   # 回刺激强度

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
        """整个闭环过程（刺激→到边→冷静→恢复→释放）零直接设备调用。"""
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
        # 停止归零也走映射派发（两路输出清零 → 默认行求值 0）
        self.assertEqual(_strength_by_channel(commands), {"A": 0, "B": 0})
        self.assertEqual(commands.reset_calls, [])
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)

    async def test_stop_without_output_change_touches_nothing(self):
        """从未派发过强度（off 模式）时停止不碰设备。"""
        bridge, commands = _bridge({"mode": "off"})
        await bridge.start()
        await asyncio.sleep(0)
        await bridge.stop()
        self.assertEqual(commands.strength_calls, [])
        self.assertEqual(commands.reset_calls, [])

    async def test_reload_config_hot_swaps_mappings(self):
        bridge, _ = _bridge()
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "max({stim_strength}, {punish_strength})")
        bridge.config["mappings"] = [
            {"param": "in_strength_a", "expr": "{punish_strength}"},
        ]
        await bridge.reload_config()
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "{punish_strength}")
        self.assertNotIn("in_strength_b", bridge.engine.mappings)


# ---------------------------------------------------------------- 插件契约

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
        self.assertEqual(meta["version"], "0.3.1")
        # 六个映射变量与 bridge PARAM_DEFS 一致
        self.assertEqual(set(meta["params"]), set(PARAM_DEFS))
        self.assertEqual(set(meta["params"]),
                         {"pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "cycles"})
        # 配置声明：玩法 / 设备绑定 / 输入映射表；纯输入模块无输出映射表
        cfg = meta["config"]
        for key in ("mode", "edge_threshold", "recovery_threshold",
                    "cooldown_s", "stim_strength", "cool_strength",
                    "ramp_s", "assist_strength", "punish_strength",
                    "punish_s", "release_s", "cycle_limit",
                    "smooth", "sensor_timeout_s", "sensor_slot",
                    "mappings"):
            self.assertIn(key, cfg)
        self.assertNotIn("pressure_pct", cfg)     # 已移除
        self.assertNotIn("phase", cfg)
        self.assertIn("assist_strength", cfg)     # 配置项与刺激强度分离
        self.assertNotIn("output_slot", cfg)      # 已移除
        self.assertNotIn("deny_zap_s", cfg)       # 直呼动作已移除
        self.assertNotIn("release_fire_s", cfg)
        self.assertNotIn("outputs", cfg)          # 纯输入设计：无回传通道
        self.assertEqual(cfg["mappings"].get("rows"), "in")
        self.assertEqual(cfg["mode"].get("choices"),
                         ["sensor", "app", "off"])
        # 按键动作静态声明与 button_actions 一致
        self.assertEqual(meta["actions"],
                         ["margin_reset_pressure", "margin_guard_toggle"])

    def test_config_defaults_cover_all_declared_keys(self):
        module = MarginControlModule()
        spec = module.config_spec()
        self.assertEqual(set(spec), set(META["config"]))
        for key, item in MARGIN_CONFIG_DEFAULTS.items():
            self.assertIn(key, spec)

    def test_link_params_returns_six_variables(self):
        module = MarginControlModule()
        params = module.link_params()
        self.assertEqual([name for name, _label in params],
                         ["pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "cycles"])

    def test_button_actions_registered(self):
        from plugins import ButtonAction

        module = MarginControlModule()
        actions = module.button_actions()
        self.assertEqual([a.key for a in actions],
                         ["margin_reset_pressure", "margin_guard_toggle"])
        self.assertTrue(all(isinstance(a, ButtonAction) for a in actions))
        self.assertTrue(all(callable(a.on_press) for a in actions))

    def test_default_mappings_target_core_inputs(self):
        """默认行引用的变量都存在，目标都是核心输入参数（映射表唯一通道）。"""
        from dglab.params import input_specs

        specs = input_specs()
        for row in DEFAULT_MAPPINGS:
            self.assertIn(row["param"], specs)
            self.assertEqual(row["expr"],
                             "max({stim_strength}, {punish_strength})")

    def test_default_expr_evaluates_per_phase(self):
        """默认表达式在状态机各阶段求值符合预期（刺激器/惩罚器取最大）。"""
        from dglab.expr import evaluate

        row = DEFAULT_MAPPINGS[0]["expr"]
        self.assertEqual(evaluate(row, {"stim_strength": 60.0,
                                        "punish_strength": 0.0}), 60.0)
        self.assertEqual(evaluate(row, {"stim_strength": 0.0,
                                        "punish_strength": 120.0}), 120.0)
        self.assertEqual(evaluate(row, {"stim_strength": 60.0,
                                        "punish_strength": 120.0}), 120.0)
        self.assertEqual(evaluate(row, {"stim_strength": 0.0,
                                        "punish_strength": 0.0}), 0.0)

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
        })())
        # 未启动桥接器时按键不抛错
        module._press_guard_toggle(None, None)
        module.on_unload()


if __name__ == "__main__":
    unittest.main()
