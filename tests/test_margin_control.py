"""灵猫边控联动模块单测（事件流驱动架构）。

覆盖：状态机事实产出（相位推进、到边/恢复/释放事件、app_N 会话事件、
持续判定与跳变、失联 fail-safe）、事件流规则引擎（where 条件、set 表达式、
定时回滚、顺序生效）、默认事件流行为（相位→刺激器输出、到边惩罚、阈值
自适应、边控 5 轮释放、官方会话接线）、映射表派发与**设备控制只经映射
表**、插件 META / link_params / 按键动作契约。不依赖真实设备。

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

from modules.margin_control.bridge import (DEFAULT_MAPPINGS, EventStream,
                                           PARAM_DEFS, EdgeGuard,
                                           MarginBridge, MarginConfig,
                                           PHASE_COOL, PHASE_IDLE,
                                           PHASE_RELEASE, PHASE_STIM)
from modules.margin_control.plugin import (MARGIN_CONFIG_DEFAULTS, META,
                                           MarginControlModule)

# 机器/桥接测试的公共兜底：关闭持续判定与跳变（除非显式覆盖）
_FAST_JUDGE = {"edge_hold_s": 0.0, "recovery_hold_s": 0.0, "jump_rise": 0.0}


class FakeCommands:
    """引擎命令层假件：记录映射派发（不触碰真实设备）。"""

    def __init__(self):
        self.state = EngineState(backend="ble")
        self.state.slots["s_out"] = Slot(slot_id="s_out", name="郊狼",
                                         type="COYOTE_030")
        self.state.slots["s_bmt"] = Slot(slot_id="s_bmt", name="灵猫",
                                         type="BMTR_010")
        self.strength_calls: list[tuple[str, int, str | None]] = []
        self.zap_calls: list[tuple] = []
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


def _bridge(config: dict | None = None, commands: FakeCommands | None = None,
            clock: float = 100.0) -> tuple[MarginBridge, FakeCommands]:
    """带假命令层的桥接器（默认事件流来自 META 声明，时钟手动推进）。"""
    commands = commands or _commands()
    merged = dict(_FAST_JUDGE)
    merged.update(config or {})
    bridge = MarginBridge(MarginConfig(merged, defaults=MARGIN_CONFIG_DEFAULTS),
                          commands.get_state, commands)
    bridge.log = lambda msg: None
    bridge._clock = lambda: clock
    bridge.tick_at = lambda t: _advance(bridge, t)
    return bridge, commands


def _advance(bridge: MarginBridge, t: float) -> None:
    bridge._clock = lambda: t
    bridge._tick()


def _steps(bridge: MarginBridge, start: float, count: int,
           delta: float = 0.1):
    """按真实节拍间隔连续推进 count 拍。"""
    t = start
    for _ in range(count):
        t += delta
        bridge.tick_at(t)
    return t


def _set_pressure(commands: FakeCommands, value: float) -> None:
    commands.state.slots["s_bmt"].pressure = value


def _strength_by_channel(commands: FakeCommands) -> dict[str, int]:
    out: dict[str, int] = {}
    for ch, value, _sid in commands.strength_calls:
        out[ch] = value
    return out


# ---------------------------------------------------------------- 状态机

class GuardTests(unittest.TestCase):
    """状态机事实产出：相位推进与事件（不做变量输出）。"""

    def _guard(self, **overrides):
        merged = dict(_FAST_JUDGE)
        merged.update(overrides)
        vars = {"phase": 0.0, "red": 17.0, "blue": 15.0, "cycles": 0.0,
                "release_req": 0.0, "phase_time": 0.0, "session_time": 0.0}
        return EdgeGuard(MarginConfig(merged), vars), vars

    def test_fresh_pressure_enters_stim(self):
        guard, vars = self._guard()
        events = guard.step(5.0, None, True, now=0.0)
        self.assertEqual(events, ["stim"])
        self.assertEqual(vars["phase"], PHASE_STIM)

    def test_crossing_red_emits_edge_and_counts(self):
        guard, vars = self._guard()
        guard.step(5.0, None, True, now=0.0)
        events = guard.step(45.0, None, True, now=1.0)
        self.assertIn("edge", events)
        self.assertEqual(vars["phase"], PHASE_COOL)
        self.assertEqual(vars["cycles"], 1)

    def test_edge_hold_requires_sustained_above(self):
        guard, vars = self._guard(edge_hold_s=2.0)
        guard.step(5.0, None, True, now=0.0)
        guard.step(20.0, None, True, now=1.0)
        self.assertEqual(vars["phase"], PHASE_STIM)     # 1s 未达
        guard.step(10.0, None, True, now=2.0)           # 回落清零
        guard.step(20.0, None, True, now=3.0)           # 重新计
        self.assertEqual(vars["phase"], PHASE_STIM)
        guard.step(20.0, None, True, now=5.1)           # 连续 2.1s → 到边
        self.assertEqual(vars["phase"], PHASE_COOL)

    def test_jump_rate_triggers_edge_below_threshold(self):
        guard, vars = self._guard(jump_rise=5.0, jump_window_s=1.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(10.0, None, True, now=0.5)
        guard.step(10.0, None, True, now=1.0)
        guard.step(16.0, None, True, now=1.5)   # 速率 6 kPa/s ≥ 5，未过红线
        self.assertEqual(vars["phase"], PHASE_COOL)

    def test_recovery_emits_recovered(self):
        guard, vars = self._guard(cooldown_s=0.0)
        guard.step(5.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)           # 到边
        events = guard.step(5.0, None, True, now=2.0)   # 低于蓝线 15
        self.assertIn("recovered", events)
        self.assertEqual(vars["phase"], PHASE_STIM)

    def test_recovery_hold_requires_sustained_below(self):
        guard, vars = self._guard(cooldown_s=0.0, recovery_hold_s=3.0)
        guard.step(5.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        guard.step(5.0, None, True, now=2.0)    # 低于蓝线 1s → 未达
        self.assertEqual(vars["phase"], PHASE_COOL)
        guard.step(5.0, None, True, now=5.1)    # 3.1s ≥ 3 → 恢复
        self.assertEqual(vars["phase"], PHASE_STIM)

    def test_release_request_consumed(self):
        guard, vars = self._guard()
        guard.step(5.0, None, True, now=0.0)
        vars["release_req"] = 1.0
        events = guard.step(5.0, None, True, now=1.0)
        self.assertIn("release", events)
        self.assertEqual(vars["phase"], PHASE_RELEASE)
        self.assertEqual(vars["release_req"], 0.0)      # 消费后清零

    def test_release_timer_returns_to_stim_and_resets_cycles(self):
        guard, vars = self._guard(release_s=1.0)
        guard.step(5.0, None, True, now=0.0)
        vars["release_req"] = 1.0
        guard.step(5.0, None, True, now=1.0)            # 进释放
        vars["cycles"] = 3.0
        events = guard.step(5.0, None, True, now=2.5)   # 释放满 1s
        self.assertIn("stim", events)
        self.assertEqual(vars["phase"], PHASE_STIM)
        self.assertEqual(vars["cycles"], 0.0)

    def test_failsafe_forces_idle_after_seen(self):
        guard, vars = self._guard()
        guard.step(5.0, None, True, now=0.0)
        guard.step(5.0, None, True, now=1.0)
        guard.step(None, None, False, now=99.0)         # 气压失联
        self.assertEqual(vars["phase"], PHASE_IDLE)

    def test_pure_app_session_not_forced_idle(self):
        """从未有过气压读数（纯官方会话）时不强制待机，app 事件照常。"""
        guard, vars = self._guard()
        events = guard.step(None, 4, False, now=0.0)
        self.assertIn("app_4", events)
        self.assertEqual(vars["phase"], PHASE_IDLE)     # 机器不动相位
        guard.step(None, 4, False, now=1.0)
        self.assertEqual(guard.step(None, 1, False, now=2.0), ["app_1"])

    def test_session_time_accumulates(self):
        guard, vars = self._guard()
        guard.step(5.0, None, True, now=0.0)
        guard.step(5.0, None, True, now=10.0)
        self.assertAlmostEqual(vars["session_time"], 10.0)
        self.assertAlmostEqual(vars["phase_time"], 10.0)


class EventStreamTests(unittest.TestCase):
    """事件流规则引擎：条件 / 赋值 / 回滚。"""

    def _stream(self) -> EventStream:
        stream = EventStream(log=lambda msg: None)
        return stream

    def test_where_min_max_bounds(self):
        stream = self._stream()
        stream.load([{"on": "tick", "where": {"phase": {"min": 1, "max": 1}},
                      "set": {"x": 1}},
                     {"on": "tick", "where": {"phase": {"min": 2}},
                      "set": {"y": 1}}])
        vars = {"phase": 1.0}
        stream.dispatch(["tick"], vars, now=0.0)
        self.assertEqual(vars, {"phase": 1.0, "x": 1.0})

    def test_where_expression_bound(self):
        stream = self._stream()
        stream.load([{"on": "tick",
                      "where": {"cycles": {"min": "{threshold}"}},
                      "set": {"hit": 1}}])
        vars = {"cycles": 5.0, "threshold": 5.0}
        stream.dispatch(["tick"], vars, now=0.0)
        self.assertEqual(vars["hit"], 1.0)
        vars["cycles"] = 4.0
        stream.dispatch(["tick"], vars, now=1.0)
        self.assertEqual(vars["hit"], 1.0)              # 未再触发

    def test_set_expressions_apply_sequentially(self):
        stream = self._stream()
        stream.load([{"on": "edge", "set": {"a": "{a} + 1"}},
                     {"on": "edge", "set": {"b": "{a} * 10"}}])
        vars = {"a": 0.0}
        stream.dispatch(["edge"], vars, now=0.0)
        self.assertEqual(vars["a"], 1.0)
        self.assertEqual(vars["b"], 10.0)               # 读到上一条的结果

    def test_revert_after_s(self):
        stream = self._stream()
        stream.load([{"on": "edge", "set": {"p": 100},
                      "revert": {"p": 0}, "after_s": 1.0}])
        vars = {"p": 0.0}
        stream.dispatch(["edge"], vars, now=0.0)
        self.assertEqual(vars["p"], 100.0)
        stream.dispatch(["tick"], vars, now=0.5)        # 未到期
        self.assertEqual(vars["p"], 100.0)
        stream.dispatch(["tick"], vars, now=1.1)        # 到期回滚
        self.assertEqual(vars["p"], 0.0)

    def test_missing_var_treated_as_zero(self):
        stream = self._stream()
        stream.load([{"on": "tick", "where": {"nothing": {"max": 1}},
                      "set": {"x": "{nothing} + 2"}}])
        vars: dict = {}
        stream.dispatch(["tick"], vars, now=0.0)
        self.assertEqual(vars["x"], 2.0)

    def test_invalid_expression_skipped(self):
        stream = self._stream()
        stream.load([{"on": "tick", "set": {"x": "1 / 0"}},
                     {"on": "tick", "set": {"y": 3}}])
        vars: dict = {}
        stream.dispatch(["tick"], vars, now=0.0)
        self.assertNotIn("x", vars)
        self.assertEqual(vars["y"], 3.0)

    def test_load_skips_invalid_rows(self):
        stream = self._stream()
        stream.load(["bad", {"set": {}}, {"on": "tick", "set": {"x": 1}}])
        self.assertEqual(len(stream.event_rules.get("tick", [])), 1)

    # ---- 新 schema：trigger = period | event + actions ----

    def test_event_trigger_with_action_revert(self):
        stream = self._stream()
        stream.load([{"name": "到边惩罚", "trigger": "event", "arg": "edge",
                      "actions": [{"var": "p", "value": 100,
                                   "revert": 0, "after_s": 1.0}]}])
        vars = {"p": 0.0}
        stream.dispatch(["edge"], vars, now=0.0)
        self.assertEqual(vars["p"], 100.0)
        stream.dispatch(["tick"], vars, now=0.5)        # 未到期
        self.assertEqual(vars["p"], 100.0)
        stream.dispatch(["tick"], vars, now=1.1)        # 到期回滚
        self.assertEqual(vars["p"], 0.0)

    def test_event_trigger_expr_action(self):
        stream = self._stream()
        stream.load([{"name": "红线下降", "trigger": "event", "arg": "edge",
                      "actions": [{"var": "red",
                                   "expr": "max(1, {red} - 1)"}]}])
        vars = {"red": 17.0}
        stream.dispatch(["edge"], vars, now=0.0)
        self.assertEqual(vars["red"], 16.0)

    def test_period_trigger_cadence_and_where(self):
        stream = self._stream()
        stream.load([{"name": "刺激期", "trigger": "period", "arg": 100,
                      "where": {"phase": {"min": 1, "max": 1}},
                      "actions": [{"var": "s", "expr": "{s} + 1"}]}])
        vars = {"phase": 1.0, "s": 0.0}
        stream.dispatch(["tick"], vars, now=0.0)        # 首拍 dt=0 不累计
        self.assertEqual(vars["s"], 0.0)
        stream.dispatch(["tick"], vars, now=0.1)        # 满 100ms → 触发
        self.assertEqual(vars["s"], 1.0)
        stream.dispatch(["tick"], vars, now=0.15)       # 未满周期
        self.assertEqual(vars["s"], 1.0)
        stream.dispatch(["tick"], vars, now=0.25)       # 累计满 → 触发
        self.assertEqual(vars["s"], 2.0)
        # 相位条件不满足时不触发
        vars["phase"] = 2.0
        stream.dispatch(["tick"], vars, now=0.4)
        stream.dispatch(["tick"], vars, now=0.55)
        self.assertEqual(vars["s"], 2.0)

    def test_invalid_typed_rows_skipped(self):
        stream = self._stream()
        stream.load([{"name": "空动作", "trigger": "period", "arg": 100,
                      "actions": []},
                     {"name": "未知触发器", "trigger": "bogus", "arg": "edge",
                      "actions": [{"var": "a", "value": 1}]},
                     {"name": "缺动作字段", "trigger": "event", "arg": "edge",
                      "actions": [{"var": "a"}]},
                     {"name": "正常", "trigger": "event", "arg": "edge",
                      "actions": [{"var": "a", "value": 1}]}])
        self.assertEqual(len(stream.period_rules), 0)
        self.assertEqual(len(stream.event_rules.get("edge", [])), 1)

    def test_legacy_on_set_rows_still_supported(self):
        """v0.5 行 schema 兼容：on/set/revert/after_s。"""
        stream = self._stream()
        stream.load([{"on": "edge", "set": {"p": 100},
                      "revert": {"p": 0}, "after_s": 1.0}])
        vars = {"p": 0.0}
        stream.dispatch(["edge"], vars, now=0.0)
        self.assertEqual(vars["p"], 100.0)
        stream.dispatch(["tick"], vars, now=1.1)
        self.assertEqual(vars["p"], 0.0)


# ---------------------------------------------------------------- 默认事件流

class DefaultStreamTests(unittest.TestCase):
    """默认事件流构造的行为（取代旧设置项选择）。"""

    def test_stim_ramp_via_tick_rule(self):
        bridge, commands = _bridge({"ramp_s": 3.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)                   # 进入刺激，phase_time=0
        self.assertEqual(bridge.vars["phase"], PHASE_STIM)
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)
        bridge.tick_at(101.5)                   # 1.5s / 3s → 30
        self.assertEqual(bridge.engine.signals["stim_strength"], 30)
        bridge.tick_at(103.5)                   # 爬满 → 60
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})

    def test_cool_phase_outputs_cool_setting(self):
        bridge, commands = _bridge({"ramp_s": 0.0, "cool_strength": 20,
                                    "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        _set_pressure(commands, 45.0)
        bridge.tick_at(110.0)                   # 到边 → 冷静
        self.assertEqual(bridge.vars["phase"], PHASE_COOL)
        self.assertEqual(bridge.engine.signals["stim_strength"], 20)

    def test_punish_on_edge_then_auto_revert(self):
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        _set_pressure(commands, 45.0)
        bridge.tick_at(110.0)                   # 到边 → 惩罚 100
        self.assertEqual(bridge.engine.signals["punish_strength"], 100)
        self.assertEqual(_strength_by_channel(commands), {"A": 100, "B": 100})
        bridge.tick_at(111.2)                   # 1s 后自动归零
        self.assertEqual(bridge.engine.signals["punish_strength"], 0)
        self.assertEqual(_strength_by_channel(commands), {"A": 0, "B": 0})

    def test_edge_adaptation_red_drop_and_blue_follow(self):
        bridge, _ = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                             "adapt_drop_pct": 10.0,
                             "adapt_blue_follow": 30.0},
                            commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.vars["red"], 17.0)
        _set_pressure(bridge_commands(bridge), 45.0)
        bridge.tick_at(110.0)                   # 到边 → 自适应
        self.assertAlmostEqual(bridge.vars["red"], 15.3)
        self.assertAlmostEqual(bridge.vars["blue"], 14.49)

    def test_timed_red_decay_after_delay(self):
        bridge, commands = _bridge({"adapt_drop_delay_s": 10.0,
                                    "adapt_drop_rate": 10.0,
                                    "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)                   # 进入刺激
        t = _steps(bridge, 100.0, 50)           # 5s < 100s 等待 → 不降
        self.assertAlmostEqual(bridge.vars["red"], 17.0)
        t = _steps(bridge, t, 600)              # 再 60s（≥ 等待）→ 持续缓降
        self.assertLess(bridge.vars["red"], 16.0)
        self.assertGreaterEqual(bridge.vars["red"], 1.0)

    def test_blue_rises_when_cool_stuck(self):
        bridge, commands = _bridge({"adapt_blue_delay_s": 10.0,
                                    "adapt_blue_rise_rate": 10.0,
                                    "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        _set_pressure(commands, 45.0)
        bridge.tick_at(110.0)                   # 到边 → 冷静
        self.assertAlmostEqual(bridge.vars["blue"], 15.0)
        t = _steps(bridge, 110.0, 600)          # 冷静 60s（≥ 等待）→ 蓝线上升
        # 恢复变容易：蓝线抬升（红线 17 → 钳到 16.5）
        self.assertGreater(bridge.vars["blue"], 15.0)
        self.assertLessEqual(bridge.vars["blue"], bridge.vars["red"] - 0.5)

    def test_release_after_five_cycles(self):
        """默认事件流：边控 5 轮后请求释放（旧「循环上限」设置的行为）。"""
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "cooldown_s": 0.0,
                                    "assist_strength": 80,
                                    "release_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)                   # 刺激
        t = 100.0
        for _ in range(5):
            _set_pressure(commands, 45.0)
            bridge.tick_at(t := t + 0.1)        # 到边
            _set_pressure(commands, 5.0)
            bridge.tick_at(t := t + 0.1)        # 恢复
        self.assertEqual(bridge.vars["cycles"], 5.0)
        self.assertEqual(bridge.vars["phase"], PHASE_STIM)
        _set_pressure(commands, 45.0)
        bridge.tick_at(t := t + 0.1)            # 第 5 次恢复时已置释放请求
        _set_pressure(commands, 5.0)
        bridge.tick_at(t := t + 0.1)            # 消费请求 → 释放
        self.assertEqual(bridge.vars["phase"], PHASE_RELEASE)
        self.assertEqual(bridge.engine.signals["on_release"], 1)
        self.assertEqual(bridge.engine.signals["stim_strength"], 80)
        self.assertEqual(_strength_by_channel(commands), {"A": 80, "B": 80})

    def test_app_session_wiring(self):
        """默认事件流：官方会话状态变化驱动相位（旧「边控模式」的选择）。"""
        bridge, commands = _bridge({}, commands=_commands(edge=1))
        bridge.tick_at(100.0)                   # app_1 → 刺激相位
        self.assertEqual(bridge.vars["phase"], PHASE_STIM)
        self.assertEqual(bridge.engine.signals["edge"], 1)
        commands.state.slots["s_bmt"].edge_state = 4
        bridge.tick_at(110.0)                   # app_4 → 释放（助力输出）
        self.assertEqual(bridge.vars["phase"], PHASE_RELEASE)
        self.assertEqual(bridge.engine.signals["on_release"], 1)
        self.assertEqual(bridge.engine.signals["stim_strength"], 80)
        commands.state.slots["s_bmt"].edge_state = 0
        bridge.tick_at(120.0)                   # app_0 → 待机
        self.assertEqual(bridge.vars["phase"], PHASE_IDLE)
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)

    def test_pure_app_without_sensor(self):
        """无气压读数（纯官方会话）时事件流照常驱动。"""
        bridge, commands = _bridge({}, commands=FakeCommands())
        commands.state.slots["s_bmt"].edge_state = 4
        bridge.tick_at(100.0)                   # app_4 → 相位 3
        self.assertEqual(bridge.vars["phase"], PHASE_RELEASE)
        bridge.tick_at(100.1)                   # 释放期周期规则生效
        self.assertEqual(bridge.engine.signals["stim_strength"], 80)

    def test_on_edge_flag(self):
        bridge, _ = _bridge({"edge_threshold": 40.0, "smooth": 0.0},
                            commands=_commands(pressure=30.0))
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)
        bridge2, _ = _bridge({"edge_threshold": 25.0, "smooth": 0.0},
                             commands=_commands(pressure=30.0))
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)

    def test_leak_compensation_offsets_pressure(self):
        bridge, _ = _bridge({"leak_comp": 3.0, "smooth": 0.0},
                            commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 13.0)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)   # 13 < 17
        bridge2, _ = _bridge({"leak_comp": 3.0, "smooth": 0.0},
                             commands=_commands(pressure=15.0))
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)  # 18 ≥ 17

    def test_empty_events_disables_output_construction(self):
        """清空事件流 = 关闭全部行为构造（安全开关）。"""
        bridge, commands = _bridge({"events": [], "ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(bridge.vars["phase"], PHASE_STIM)  # 机器事实照常
        self.assertEqual(bridge.engine.signals.get("stim_strength"), 0)
        self.assertEqual(commands.strength_calls, [])

    def test_custom_event_rule(self):
        bridge, _ = _bridge({"events": [
            {"name": "常量输出", "trigger": "period", "arg": 100,
             "actions": [{"var": "stim_strength",
                          "expr": "{stim_setting}"}]}],
        }, commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)                   # 首拍 dt=0，周期未满
        bridge.tick_at(100.1)                   # 周期触发
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)

    def test_custom_legacy_rule_still_works(self):
        bridge, _ = _bridge({"events": [
            {"on": "tick", "set": {"stim_strength": "{stim_setting}"}}],
        }, commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["stim_strength"], 60)

    def test_temps_seeded_and_usable(self):
        """temps 播种临时变量：规则可引用，重载不覆盖已有值。"""
        bridge, _ = _bridge({"temps": [{"name": "my_count", "value": 5}],
                             "events": [
            {"name": "用临时变量", "trigger": "period", "arg": 100,
             "actions": [{"var": "stim_strength",
                          "expr": "{my_count} * 10"}]}],
        }, commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(bridge.vars["my_count"], 5.0)
        bridge.tick_at(100.1)                   # 周期触发
        self.assertEqual(bridge.engine.signals["stim_strength"], 50)
        bridge.vars["my_count"] = 9.0
        bridge._seed_temps()                    # 重载播种不覆盖已有值
        self.assertEqual(bridge.vars["my_count"], 9.0)

    def test_blue_invariant_clamped_by_bridge(self):
        bridge, _ = _bridge({"events": [
            {"name": "蓝线越界", "trigger": "period", "arg": 100,
             "actions": [{"var": "blue", "value": 50}]}]},
            commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        bridge.tick_at(100.1)
        self.assertAlmostEqual(bridge.vars["blue"], 16.5)   # ≤ 红线 − 0.5


def bridge_commands(bridge: MarginBridge) -> FakeCommands:
    """取桥接器持有的命令假件（测试辅助）。"""
    return bridge.commands


# ---------------------------------------------------------------- 桥接器

class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_sensor_slot_binding(self):
        commands = _commands(pressure=10.0)
        commands.state.slots["s_bmt2"] = Slot(slot_id="s_bmt2", name="灵猫2",
                                              type="BMTR_020")
        commands.state.slots["s_bmt2"].pressure = 20.0
        bridge, _ = _bridge({"sensor_slot": "s_bmt2"}, commands)
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 20.0)

    def test_mapping_dispatch_targets_family_first_device(self):
        commands = _commands(pressure=10.0)
        commands.state.slots["s_out2"] = Slot(slot_id="s_out2", name="郊狼2",
                                              type="COYOTE_031")
        bridge, _ = _bridge({"ramp_s": 0.0}, commands)
        bridge.tick_at(100.0)
        bridge.tick_at(103.5)                       # 爬满 60
        self.assertTrue(all(sid == "s_out"          # 家族内排序第一台
                            for _ch, _v, sid in commands.strength_calls))

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
        self.assertEqual(bridge.vars["phase"], PHASE_IDLE)
        self.assertEqual(bridge.engine.signals.get("stim_strength"), 0)
        self.assertEqual(commands.strength_calls, [])

    def test_pause_zeroes_outputs_via_mapping(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        bridge.tick_at(103.5)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        self.assertTrue(bridge.toggle_pause())
        bridge.tick_at(110.0)
        self.assertEqual(bridge.vars["phase"], PHASE_IDLE)
        self.assertEqual(_strength_by_channel(commands), {"A": 0, "B": 0})
        self.assertFalse(bridge.toggle_pause())

    def test_no_direct_device_calls_ever(self):
        """整个闭环（刺激→到边→冷静→恢复→…→释放）零直接设备调用。"""
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "cooldown_s": 0.0, "release_s": 0.1},
                                   commands=_commands(pressure=10.0))
        t = 100.0
        for _ in range(7):
            _set_pressure(commands, 45.0)
            bridge.tick_at(t := t + 0.1)
            _set_pressure(commands, 5.0)
            bridge.tick_at(t := t + 0.1)
        bridge.toggle_pause()
        bridge.tick_at(t + 1.0)
        self.assertEqual(commands.zap_calls, [])
        self.assertEqual(commands.fire_calls, [])
        self.assertEqual(commands.reset_calls, [])

    async def test_stop_zeroes_via_mapping_only(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        await bridge.start()
        bridge.tick_at(100.0)
        bridge.tick_at(103.5)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        await bridge.stop()
        self.assertEqual(_strength_by_channel(commands), {"A": 0, "B": 0})
        self.assertEqual(commands.reset_calls, [])
        self.assertEqual(bridge.engine.signals["stim_strength"], 0)

    async def test_stop_without_output_change_touches_nothing(self):
        bridge, commands = _bridge({"events": []})
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

    async def test_reload_config_refreshes_mirrors_keeps_adaptation(self):
        bridge, _ = _bridge({"ramp_s": 0.0, "adapt_drop_pct": 10.0,
                             "smooth": 0.0},
                            commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        _set_pressure(bridge.commands, 45.0)
        bridge.tick_at(110.0)                       # 红线自适应 17 → 15.3
        self.assertAlmostEqual(bridge.vars["red"], 15.3)
        bridge.config["stim_strength"] = 100
        await bridge.reload_config()
        self.assertEqual(bridge.vars["stim_setting"], 100.0)  # 镜像热更新
        self.assertAlmostEqual(bridge.vars["red"], 15.3)      # 自适应保留


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
        self.assertEqual(meta["version"], "0.6.0")
        # 变量表与 bridge PARAM_DEFS 一致（事实 + 输出 + 设置镜像）
        self.assertEqual(set(meta["params"]), set(PARAM_DEFS))
        # 配置声明：基础 / 判定条件 / 强度 / 自适应取值 / 事件流 / 临时变量
        cfg = meta["config"]
        for key in ("sensor_slot", "smooth", "sensor_timeout_s", "leak_comp",
                    "edge_threshold", "recovery_threshold", "edge_hold_s",
                    "jump_rise", "jump_window_s", "cooldown_s",
                    "recovery_hold_s", "release_s",
                    "stim_strength", "cool_strength", "assist_strength",
                    "ramp_s",
                    "adapt_drop_pct", "adapt_drop_rate", "adapt_drop_delay_s",
                    "adapt_blue_follow", "adapt_blue_rise_rate",
                    "adapt_blue_delay_s",
                    "events", "temps", "mappings"):
            self.assertIn(key, cfg)
        # 选择类设置项已移除（行为由默认事件流构造）
        for removed in ("mode", "punish_strength", "punish_s", "cycle_limit",
                        "time_release_s", "adapt_stim", "adapt_cool"):
            self.assertNotIn(removed, cfg)
        self.assertNotIn("outputs", cfg)
        self.assertEqual(cfg["mappings"].get("rows"), "in")
        # 默认事件流：非空，每行含 name/trigger/arg/actions
        events = cfg["events"]["default"]
        self.assertTrue(events)
        for row in events:
            self.assertIn("name", row)
            self.assertIn("trigger", row)
            self.assertIn("arg", row)
            self.assertIn("actions", row)
            self.assertTrue(row["actions"])
        # 按键动作静态声明与 button_actions 一致
        self.assertEqual(meta["actions"],
                         ["margin_reset_pressure", "margin_guard_toggle"])

    def test_default_events_construct_previous_choices(self):
        """默认事件流覆盖旧设置项的全部行为选择。"""
        events = META["config"]["events"]["default"]
        # 官方会话接线（旧 mode 选择）
        wired = {str(row["arg"]) for row in events
                 if row["trigger"] == "event"
                 and str(row["arg"]).startswith("app_")}
        self.assertEqual(wired, {"app_0", "app_1", "app_2", "app_3", "app_4"})
        # 到边惩罚（旧 punish_strength/punish_s 设置）
        punish = next(row for row in events
                      if row["arg"] == "edge"
                      and any(a["var"] == "punish_strength"
                              for a in row["actions"]))
        action = punish["actions"][0]
        self.assertEqual(action["value"], 100)
        self.assertEqual(action["revert"], 0)
        self.assertEqual(action["after_s"], 1.0)
        # 边控 5 轮释放（旧 cycle_limit 设置）
        release = next(row for row in events if row["arg"] == "recovered")
        self.assertEqual(release["where"], {"cycles": {"min": 5}})
        self.assertEqual(release["actions"],
                         [{"var": "release_req", "value": 1}])
        # 相位周期规则（刺激/冷静/释放/待机 → 刺激器输出）
        period_names = {row["name"] for row in events
                        if row["trigger"] == "period"}
        self.assertTrue({"刺激期", "冷静期", "释放期", "待机期"}
                        <= period_names)
        # 自适应缓降/回升规则在对应相位上
        self.assertIn("刺激期红线缓降", period_names)
        self.assertIn("冷静期蓝线回升", period_names)

    def test_config_defaults_cover_all_declared_keys(self):
        module = MarginControlModule()
        spec = module.config_spec()
        self.assertEqual(set(spec), set(META["config"]))
        for key, item in MARGIN_CONFIG_DEFAULTS.items():
            self.assertIn(key, spec)

    def test_link_params_returns_variable_table(self):
        module = MarginControlModule()
        params = module.link_params()
        self.assertEqual([name for name, _label in params],
                         list(PARAM_DEFS))
        self.assertEqual(len(params), 24)

    def test_button_actions_registered(self):
        from plugins import ButtonAction

        module = MarginControlModule()
        actions = module.button_actions()
        self.assertEqual([a.key for a in actions],
                         ["margin_reset_pressure", "margin_guard_toggle"])
        self.assertTrue(all(isinstance(a, ButtonAction) for a in actions))
        self.assertTrue(all(callable(a.on_press) for a in actions))

    def test_default_mappings_target_core_inputs(self):
        from dglab.params import input_specs

        specs = input_specs()
        for row in DEFAULT_MAPPINGS:
            self.assertIn(row["param"], specs)
            self.assertEqual(row["expr"],
                             "max({stim_strength}, {punish_strength})")

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
        module._press_guard_toggle(None, None)
        module.on_unload()


if __name__ == "__main__":
    unittest.main()
