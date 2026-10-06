"""灵猫边控联动模块单测。

覆盖：闭环状态机纯逻辑（红线/蓝线 + 持续判定 + 气压跳变 + 阈值自适应 +
循环上限/持续时长释放、app 跟随官方边控状态 0-4、爬升与失联 fail-safe、
惩罚窗口）、桥接器一拍推进（七个映射变量喂入 + 默认行刺激器/惩罚器最大
值派发 + 停止经映射表归零）、映射表默认行与热更新、**设备控制只经映射
表**（不直接调用 zap/fire/reset）、插件 META / link_params / 按键动作契约。
不依赖真实设备（引擎命令层用假件记录派发）。

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

# 状态机/桥接测试的公共兜底：关闭持续判定与跳变（除非显式覆盖），
# 使既有时间线不受新判定条件默认值影响
_FAST_JUDGE = {"edge_hold_s": 0.0, "recovery_hold_s": 0.0, "jump_rise": 0.0}


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
    merged = dict(_FAST_JUDGE)
    merged.update(config or {})
    bridge = MarginBridge(MarginConfig(merged), commands.get_state, commands)
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
    """sensor 模式：气压闭环（红线/蓝线判定 → 冷静 → 恢复 / 释放）。"""

    def _guard(self, **overrides) -> EdgeGuard:
        merged = dict(_FAST_JUDGE)
        merged.update(overrides)
        return EdgeGuard(MarginConfig(merged))

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
        # 气压越过红线 17 → 冷静 + 记一次循环 + 撤除刺激
        guard.step(41.0, None, True, now=1.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertEqual(guard.cycles, 1)
        self.assertEqual(guard.outputs(2.5), (0, 0))      # 惩罚窗口(1s)已过

    # ---- 持续判定（官方「连续 N 秒高于红线/低于蓝线」） ----

    def test_edge_hold_requires_sustained_above(self):
        guard = self._guard(edge_hold_s=2.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(20.0, None, True, now=1.0)     # 高于红线 1s → 未达
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(20.0, None, True, now=2.5)     # 1.5s → 未达
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(20.0, None, True, now=3.1)     # 2.1s ≥ 2 → 到边
        self.assertEqual(guard.phase, PHASE_COOL)

    def test_edge_hold_resets_when_below(self):
        guard = self._guard(edge_hold_s=2.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(20.0, None, True, now=1.0)     # 高于 1s
        guard.step(10.0, None, True, now=2.0)     # 回落清零
        guard.step(20.0, None, True, now=3.0)     # 重新计 0s
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(20.0, None, True, now=5.1)     # 连续 2.1s → 到边
        self.assertEqual(guard.phase, PHASE_COOL)

    def test_recovery_hold_requires_sustained_below(self):
        guard = self._guard(recovery_hold_s=3.0, cooldown_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)     # 到边进冷静
        guard.step(5.0, None, True, now=2.0)      # 低于蓝线 1s → 未达
        self.assertEqual(guard.phase, PHASE_COOL)
        guard.step(5.0, None, True, now=4.0)      # 2s → 未达
        self.assertEqual(guard.phase, PHASE_COOL)
        guard.step(5.0, None, True, now=5.1)      # 3.1s ≥ 3 → 恢复
        self.assertEqual(guard.phase, PHASE_STIM)

    # ---- 气压跳变（官方「气压短时间上升差值」） ----

    def test_jump_rate_triggers_edge_below_threshold(self):
        guard = self._guard(jump_rise=5.0, jump_window_s=1.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(10.0, None, True, now=0.5)
        guard.step(10.0, None, True, now=1.0)
        # 1 秒窗口内 10 → 16（速率 6 kPa/s ≥ 5），虽未过红线 17 也判到边
        guard.step(16.0, None, True, now=1.5)
        self.assertEqual(guard.phase, PHASE_COOL)

    def test_jump_rate_below_threshold_no_trigger(self):
        guard = self._guard(jump_rise=5.0, jump_window_s=1.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(10.0, None, True, now=0.5)
        guard.step(13.0, None, True, now=1.5)     # 速率 3 kPa/s < 5
        self.assertEqual(guard.phase, PHASE_STIM)

    # ---- 惩罚窗口 / 冷静期 ----

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
        guard = self._guard(cooldown_s=10.0, recovery_threshold=20.0)
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

    # ---- 阈值自适应（官方「阈值自适应调整」） ----

    def test_adaptive_red_drops_after_edge_and_blue_follows(self):
        guard = self._guard(adapt_drop_pct=10.0, adapt_blue_follow=30.0)
        guard.step(10.0, None, True, now=0.0)
        self.assertAlmostEqual(guard.red_threshold(), 17.0)
        self.assertAlmostEqual(guard.blue_threshold(), 15.0)
        guard.step(45.0, None, True, now=1.0)     # 成功边控
        # 红线下降 17×10% = 1.7，但下限 15 + 0.5 = 15.5：只降 1.5；
        # 蓝线跟随 1.5×30% = 0.45 → 14.55
        self.assertAlmostEqual(guard.red_threshold(), 15.5)
        self.assertAlmostEqual(guard.blue_threshold(), 14.55)

    def test_adaptive_red_timed_drop_when_no_edge(self):
        guard = self._guard(adapt_drop_delay_s=10.0, adapt_drop_rate=10.0)
        guard.step(10.0, None, True, now=0.0)     # STIM
        guard.step(10.0, None, True, now=5.0)     # 未到等待时长 → 不降
        self.assertAlmostEqual(guard.red_threshold(), 17.0)
        guard.step(10.0, None, True, now=15.0)    # dt=10（截 5s）
        # 线性缓降 17×10%/s×5 = 8.5，但下限 15.5 钳住
        self.assertAlmostEqual(guard.red_threshold(), 15.5)

    def test_adaptive_blue_rises_when_cool_stuck(self):
        guard = self._guard(adapt_blue_delay_s=10.0,
                            adapt_blue_rise_rate=10.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)     # 冷静（phase_since=1）
        guard.step(15.5, None, True, now=5.0)     # 未到等待 → 蓝线不动
        self.assertAlmostEqual(guard.blue_threshold(), 15.0)
        guard.step(15.5, None, True, now=20.0)    # dt=15（截 5s）→ +7.5
        # 蓝线 22.5 越过红线 → 钳到红线 − 0.5 = 16.5（恢复变容易）
        self.assertAlmostEqual(guard.blue_threshold(), 16.5)
        self.assertEqual(guard.phase, PHASE_STIM) # 15.5 ≤ 16.5 → 已恢复

    def test_adaptive_disabled_by_toggles(self):
        guard = self._guard(adapt_stim=False, adapt_drop_pct=20.0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertAlmostEqual(guard.red_threshold(), 17.0)   # 开关关闭不降
        guard2 = self._guard(adapt_cool=False, adapt_blue_delay_s=0.0,
                             adapt_blue_rise_rate=100.0)
        guard2.step(10.0, None, True, now=0.0)
        guard2.step(45.0, None, True, now=1.0)
        guard2.step(45.0, None, True, now=3.0)
        self.assertAlmostEqual(guard2.blue_threshold(), 15.0)

    # ---- 释放触发（次数 / 持续时长） ----

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

    def test_time_release_after_session_duration(self):
        """官方「游戏进行指定时长后，允许高潮释放」。"""
        guard = self._guard(time_release_s=30.0, assist_strength=80)
        guard.step(10.0, None, True, now=0.0)     # 会话开始（首次进刺激）
        guard.step(10.0, None, True, now=29.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        guard.step(10.0, None, True, now=31.0)    # 会话 31s ≥ 30 → 释放
        self.assertEqual(guard.phase, PHASE_RELEASE)
        self.assertEqual(guard.outputs(31.0), (80, 0))

    def test_time_release_also_applies_at_cool_completion(self):
        guard = self._guard(time_release_s=30.0, cooldown_s=5.0,
                            recovery_threshold=20.0, cycle_limit=0)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=10.0)    # 到边（会话 10s）
        guard.step(5.0, None, True, now=40.0)     # 冷静期满且会话 ≥ 30s
        self.assertEqual(guard.phase, PHASE_RELEASE)

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

    def test_release_conditions_not_applied_in_app_mode(self):
        """app 模式释放只由会话状态 4 决定，次数/时长条件不介入。"""
        guard = self._guard(cooldown_s=0.0, recovery_threshold=20.0,
                            cycle_limit=1, release_s=15.0,
                            time_release_s=30.0)
        guard.step(10.0, 1, True, now=0.0)
        guard.step(10.0, 2, True, now=1.0)        # cycles=1 已达上限
        guard.step(10.0, 1, True, now=40.0)       # 会话 40s 已达时长
        self.assertEqual(guard.phase, PHASE_STIM)  # 仍由会话状态决定


# ---------------------------------------------------------------- 桥接器

class BridgeTickTests(unittest.IsolatedAsyncioTestCase):
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

    # ---- 问题 1 回归：红线自适应有界（不再无声跌破设定阈值） ----

    def test_red_timed_decay_is_linear_and_floored(self):
        """缓降按配置基线线性计算，且红线永不跌破恢复阈值 + 间隙。"""
        bridge, commands = _bridge({"adapt_drop_delay_s": 10.0,
                                    "adapt_drop_rate": 10.0,
                                    "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)                   # 进入刺激
        # 连续推进 600s（远超等待时长）：旧实现会指数衰减到 1.0 附近
        t = 100.0
        for _ in range(6000):
            t += 0.1
            bridge.tick_at(t)
        red = bridge.guard.red_threshold()
        # 线性：17 × 10%/s × dt，但下限 15 + 0.5 = 15.5
        self.assertAlmostEqual(red, 15.5)
        # 下限钳制：到边判定永不低于恢复阈值 + 0.5
        self.assertGreaterEqual(red, bridge.guard.blue_threshold() + 0.5)

    def test_red_edge_drop_respects_floor(self):
        bridge, commands = _bridge({"adapt_drop_pct": 90.0,
                                    "adapt_blue_follow": 0.0, "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)                   # 刺激（since=100）
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)                   # 到边：17×90% 想降 15.3
        # 下限 15.5：只允许降到 15.5
        self.assertAlmostEqual(bridge.guard.red_threshold(), 15.5)
        self.assertGreaterEqual(bridge.guard.red_threshold(),
                                bridge.guard.blue_threshold() + 0.5)

    # ---- 问题 2 回归：派发目标不跨设备串扰 ----

    def test_defaults_follow_available_output_device(self):
        """默认行跟随实际输出设备：郊狼离线时负鼠接管（in_ovc_* 行），
        且旧郊狼行被归零，不留残留强度。"""
        commands = _commands(pressure=10.0)
        commands.state.slots["s_ovc"] = Slot(slot_id="s_ovc", name="负鼠",
                                             type="OVC_1")
        bridge, commands = _bridge({"ramp_s": 0.0}, commands)
        bridge.tick_at(100.0)                   # 郊狼在线 → 郊狼 A/B
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        self.assertTrue(all(sid == "s_out"
                            for _ch, _v, sid in commands.strength_calls))
        del commands.state.slots["s_out"]       # 郊狼离线
        n_before = len(commands.strength_calls)
        _run = bridge.reload_config()
        _drain = asyncio.new_event_loop()
        try:
            _drain.run_until_complete(_run)
        finally:
            _drain.close()
        bridge.tick_at(110.0)
        # 负鼠接管：收到刺激强度；切换后旧郊狼通道只收到归零派发
        after = commands.strength_calls[n_before:]
        by_device: dict[str, list[tuple[str, int]]] = {}
        for ch, v, sid in after:
            by_device.setdefault(sid, []).append((ch, v))
        self.assertIn("s_ovc", by_device)
        self.assertEqual(sorted(by_device["s_ovc"]),
                         [("A", 60), ("B", 60)])
        if "s_out" in by_device:
            self.assertTrue(all(v == 0 for _ch, v in by_device["s_out"]),
                            by_device["s_out"])

    def test_output_slot_by_name_binding(self):
        """绑定支持设备名包含匹配（不区分大小写）。"""
        commands = _commands(pressure=10.0)
        commands.state.slots["s_ovc"] = Slot(slot_id="s_ovc",
                                             name="负鼠 OVC 振动",
                                             type="OVC_1")
        bridge, commands = _bridge({"ramp_s": 0.0, "output_slot": "负鼠"},
                                   commands)
        bridge.tick_at(100.0)
        self.assertTrue(commands.strength_calls)
        self.assertTrue(all(sid == "s_ovc"
                            for _ch, _v, sid in commands.strength_calls))

    def test_default_rows_use_ovc_params_for_ovc_only(self):
        """只有负鼠在线（无绑定）时，默认行自动改用 in_ovc_* 参数。"""
        commands = _commands(pressure=10.0)
        del commands.state.slots["s_out"]       # 仅负鼠
        commands.state.slots["s_ovc"] = Slot(slot_id="s_ovc", name="负鼠",
                                             type="OVC_1")
        bridge, commands = _bridge({"ramp_s": 0.0}, commands)
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.mappings.get("in_ovc_strength_a"),
                         "max({stim_strength}, {punish_strength})")
        self.assertNotIn("in_strength_a", bridge.engine.mappings)
        self.assertTrue(all(sid == "s_ovc"
                            for _ch, _v, sid in commands.strength_calls))

    def test_target_change_logged(self):
        """派发目标变化时记日志（落点可见）。"""
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        logs: list[str] = []
        bridge.log = logs.append
        bridge.tick_at(100.0)
        self.assertTrue(any("派发目标" in msg for msg in logs))

    def test_output_slot_binding_drives_bound_device(self):
        """绑定目标输出设备后，全部强度行都驱动绑定设备。"""
        commands = _commands(pressure=10.0)
        commands.state.slots["s_ovc"] = Slot(slot_id="s_ovc", name="负鼠",
                                             type="OVC_1")
        bridge, commands = _bridge({"ramp_s": 0.0, "output_slot": "s_ovc"},
                                   commands)
        bridge.tick_at(100.0)
        self.assertTrue(commands.strength_calls)
        self.assertTrue(all(sid == "s_ovc"
                            for _ch, _v, sid in commands.strength_calls))

    def test_output_slot_ignores_sensor_binding(self):
        """绑定到传感器（BMTR）不生效，回落家族解析。"""
        bridge, commands = _bridge({"ramp_s": 0.0,
                                    "output_slot": "s_bmt"},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertTrue(all(sid == "s_out"
                            for _ch, _v, sid in commands.strength_calls))

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
        """漏气补偿叠加到读数参与判定与映射变量。"""
        bridge, _ = _bridge({"leak_comp": 3.0, "smooth": 0.0,
                             "edge_threshold": 17.0},
                            commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 13.0)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)   # 13 < 17
        bridge2, _ = _bridge({"leak_comp": 3.0, "smooth": 0.0},
                             commands=_commands(pressure=15.0))
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)  # 18 ≥ 17

    def test_on_release_flag_follows_phase(self):
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "cooldown_s": 0.0,
                                    "recovery_threshold": 20.0,
                                    "cycle_limit": 1, "release_s": 0.0,
                                    "assist_strength": 90},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(bridge.engine.signals["on_release"], 0)
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)                     # 到边 → 冷静
        self.assertEqual(bridge.engine.signals["on_release"], 0)
        commands.state.slots["s_bmt"].pressure = 5.0
        bridge.tick_at(120.0)                     # 冷静期满 → 释放
        self.assertEqual(bridge.engine.signals["on_release"], 1)
        self.assertEqual(_strength_by_channel(commands), {"A": 90, "B": 90})

    def test_crossing_drives_punish_through_mapping(self):
        """到边 → 冷静 + 惩罚窗口：默认行派发惩罚器强度，窗口过后归零。"""
        bridge, commands = _bridge({"ramp_s": 0.0, "smooth": 0.0,
                                    "punish_strength": 120,
                                    "punish_s": 1.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual(_strength_by_channel(commands), {"A": 60, "B": 60})
        # 气压升到红线之上 → 冷静 + 惩罚输出经映射行派发
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
        self.assertEqual(meta["version"], "0.8.1")
        # 七个映射变量与 bridge PARAM_DEFS 一致
        self.assertEqual(set(meta["params"]), set(PARAM_DEFS))
        self.assertEqual(set(meta["params"]),
                         {"pressure", "edge", "stim_strength",
                          "punish_strength", "on_edge", "on_release",
                          "cycles"})
        # 配置声明：基础 / 判定条件 / 释放 / 强度 / 阈值自适应 / 映射表
        cfg = meta["config"]
        for key in ("mode", "sensor_slot", "output_slot", "smooth",
                    "sensor_timeout_s",
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
        self.assertNotIn("phase", cfg)            # 已移除
        self.assertNotIn("pressure_pct", cfg)
        self.assertNotIn("deny_zap_s", cfg)       # 直呼动作已移除
        self.assertNotIn("release_fire_s", cfg)
        self.assertNotIn("outputs", cfg)          # 纯输入设计：无回传通道
        self.assertEqual(cfg["mappings"].get("rows"), "in")
        self.assertEqual(cfg["mode"].get("choices"),
                         ["sensor", "app", "off"])
        # 设置项分组对齐官方设置页
        self.assertEqual(cfg["edge_threshold"].get("group"), "judge")
        self.assertEqual(cfg["cycle_limit"].get("group"), "release")
        self.assertEqual(cfg["adapt_drop_pct"].get("group"), "adapt")
        # 按键动作静态声明与 button_actions 一致
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
