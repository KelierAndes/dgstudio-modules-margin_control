"""灵猫边控联动模块单测。

覆盖：闭环状态机纯逻辑（sensor 气压闭环的到边/冷静/恢复、app 跟随官方
边控状态 0-4、爬升与失联 fail-safe、到边惩罚/释放开火动作）、桥接器一拍
推进（映射变量喂入 + 默认行强度派发 + 停止归零）、映射表默认行与热更新、
插件 META / link_params / 按键动作契约。不依赖真实设备（引擎命令层用
假件记录派发）。

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
    """引擎命令层假件：记录派发与动作（不触碰真实设备）。"""

    def __init__(self):
        self.state = EngineState(backend="ble")
        self.state.slots["s_out"] = Slot(slot_id="s_out", name="郊狼",
                                         type="COYOTE_030")
        self.state.slots["s_bmt"] = Slot(slot_id="s_bmt", name="灵猫",
                                         type="BMTR_010")
        self.strength_calls: list[tuple[str, int, str | None]] = []
        self.zap_calls: list[tuple[str, float, str | None]] = []
        self.fire_calls: list[tuple[str | None, float | None]] = []
        self.reset_calls: list[tuple[str, str | None]] = []
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

    def reset_strength(self, channel, slot_id=None):
        self.reset_calls.append((channel, slot_id))

        async def _noop():
            pass
        return _noop()

    def zap(self, channel, seconds=1.0, slot_id=None):
        self.zap_calls.append((channel, float(seconds), slot_id))

        async def _noop():
            pass
        return _noop()

    def fire(self, slot_id=None, duration_s=None, channel=None):
        self.fire_calls.append((slot_id, duration_s))

        async def _noop():
            pass
        return _noop()

    def set_wave(self, channel, name, slot_id=None):
        async def _noop():
            pass
        return _noop()

    def fire_start(self, slot_id=None, channel=None):
        async def _noop():
            pass
        return _noop()

    def fire_stop(self, slot_id=None, channel=None):
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
    """带假命令层的桥接器（时钟固定，测试里手动推进）。"""
    commands = commands or _commands()
    bridge = MarginBridge(MarginConfig(config or {}), commands.get_state,
                          commands)
    bridge.log = lambda msg: None
    now = clock

    def _clock():
        return now

    bridge._clock = _clock
    bridge.tick_at = lambda t: _advance(bridge, t)
    return bridge, commands


def _advance(bridge: MarginBridge, t: float):
    bridge._clock = lambda: t
    bridge._tick()
    # 同步测试里 _spawn 无事件循环可挂，协程直接 close，无需冲刷任务


def _steps(start: float, count: int, delta: float = 0.5):
    return [start + i * delta for i in range(1, count + 1)]


# ---------------------------------------------------------------- 状态机

class GuardSensorTests(unittest.TestCase):
    """sensor 模式：气压闭环（到边 → 冷静 → 恢复）。"""

    def _guard(self, **overrides) -> EdgeGuard:
        config = MarginConfig(overrides or {})
        return EdgeGuard(config)

    def test_starts_stimulating_when_pressure_fresh(self):
        guard = self._guard()
        actions = guard.step(5.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        self.assertEqual(actions, [])

    def test_stays_idle_without_fresh_pressure(self):
        guard = self._guard()
        guard.step(5.0, None, False, now=0.0)
        self.assertEqual(guard.phase, PHASE_IDLE)
        self.assertEqual(guard.drive, 0)

    def test_stays_idle_in_off_mode(self):
        guard = self._guard(mode="off")
        guard.step(50.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_IDLE)
        self.assertEqual(guard.drive, 0)

    def test_crossing_threshold_cools_and_counts(self):
        guard = self._guard(ramp_s=0.0)
        guard.step(10.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_STIM)
        self.assertEqual(guard.drive, 60)
        # 气压越过边缘阈值 40 → 冷静 + 记一次循环 + 撤除刺激
        actions = guard.step(41.0, None, True, now=1.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertEqual(guard.cycles, 1)
        self.assertEqual(guard.drive, 0)
        self.assertEqual(actions, [])            # deny_zap_s 缺省关闭

    def test_deny_zap_fires_once_on_entry(self):
        guard = self._guard(deny_zap_s=1.5)
        guard.step(10.0, None, True, now=0.0)
        guard.step(45.0, None, True, now=1.0)
        self.assertEqual(guard.last_actions, [("zap", 1.5)])
        # 冷静期内重复到边不再触发
        guard.step(50.0, None, True, now=2.0)
        self.assertEqual(guard.last_actions, [])

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
        self.assertEqual(guard.drive, 60)
        guard.step(None, None, False, now=99.0)   # 气压失联
        self.assertEqual(guard.phase, PHASE_IDLE)
        self.assertEqual(guard.drive, 0)

    def test_ramp_climbs_to_target(self):
        guard = self._guard(ramp_s=10.0, stim_strength=100)
        guard.step(10.0, None, True, now=0.0)
        self.assertEqual(guard.drive, 0)          # 爬升起点
        guard.step(10.0, None, True, now=5.0)
        self.assertEqual(guard.drive, 50)         # 中点
        guard.step(10.0, None, True, now=12.0)
        self.assertEqual(guard.drive, 100)        # 爬满

    def test_offline_then_online_reenters_stim(self):
        guard = self._guard()
        guard.step(10.0, None, True, now=0.0)
        guard.step(None, None, False, now=1.0)
        self.assertEqual(guard.phase, PHASE_IDLE)
        guard.step(10.0, None, True, now=2.0)
        self.assertEqual(guard.phase, PHASE_STIM)


class GuardAppTests(unittest.TestCase):
    """app 模式：跟随官方边控会话（edgeState 0-4）。"""

    def _guard(self, **overrides) -> EdgeGuard:
        return EdgeGuard(MarginConfig({"mode": "app", **overrides}))

    def test_state_map(self):
        guard = self._guard(ramp_s=0.0, release_strength=80)
        guard.step(10.0, 1, True, now=0.0)
        self.assertEqual((guard.phase, guard.drive), (PHASE_STIM, 60))
        guard.step(10.0, 2, True, now=1.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertEqual(guard.cycles, 1)
        guard.step(10.0, 3, True, now=2.0)
        self.assertEqual(guard.phase, PHASE_COOL)
        self.assertEqual(guard.cycles, 1)         # 判定不计新循环
        guard.step(10.0, 4, True, now=3.0)
        self.assertEqual((guard.phase, guard.drive),
                         (PHASE_RELEASE, 80))
        guard.step(10.0, 0, True, now=4.0)
        self.assertEqual((guard.phase, guard.drive), (PHASE_IDLE, 0))

    def test_release_fire_on_entry(self):
        guard = self._guard(release_fire_s=6.0)
        guard.step(10.0, 1, True, now=0.0)
        guard.step(10.0, 4, True, now=1.0)
        self.assertEqual(guard.last_actions, [("fire", 6.0)])
        guard.step(10.0, 4, True, now=2.0)
        self.assertEqual(guard.last_actions, [])  # 重复进入不重复开火

    def test_missing_session_stays_idle(self):
        guard = self._guard()
        guard.step(10.0, None, True, now=0.0)
        self.assertEqual(guard.phase, PHASE_IDLE)

    def test_ramp_applies_in_app_mode(self):
        guard = self._guard(ramp_s=10.0, stim_strength=100)
        guard.step(10.0, 1, True, now=0.0)
        self.assertEqual(guard.drive, 0)
        guard.step(10.0, 1, True, now=10.0)
        self.assertEqual(guard.drive, 100)


# ---------------------------------------------------------------- 桥接器

class BridgeTickTests(unittest.IsolatedAsyncioTestCase):
    def test_tick_feeds_variables_and_drives_strength(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure"], 10.0)
        self.assertEqual(bridge.engine.signals["phase"], PHASE_STIM)
        self.assertEqual(bridge.engine.signals["drive"], 60)
        # 默认行：{drive} 驱动 A/B 强度
        self.assertEqual(len(commands.strength_calls), 2)
        channels = {ch: value for ch, value, _sid in commands.strength_calls}
        self.assertEqual(channels, {"A": 60, "B": 60})

    def test_pressure_pct_and_on_edge(self):
        bridge, _ = _bridge({}, commands=_commands(pressure=30.0))
        bridge.tick_at(100.0)
        self.assertAlmostEqual(bridge.engine.signals["pressure_pct"], 50.0,
                               delta=0.1)
        self.assertEqual(bridge.engine.signals["on_edge"], 0)
        bridge2, _ = _bridge({"edge_threshold": 25.0},
                             commands=_commands(pressure=30.0))
        bridge2.tick_at(100.0)
        self.assertEqual(bridge2.engine.signals["on_edge"], 1)

    def test_crossing_triggers_zap_and_zeroes_strength(self):
        bridge, commands = _bridge({"ramp_s": 0.0, "deny_zap_s": 1.0,
                                    "smooth": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertEqual([ch for ch, _v, _sid in commands.strength_calls],
                         ["A", "B"])
        # 气压升到阈值之上 → 冷静 + 双通道惩罚脉冲 + 强度归零派发
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)
        self.assertEqual({(ch, sec) for ch, sec, _sid in commands.zap_calls},
                         {("A", 1.0), ("B", 1.0)})
        last = {(ch, v) for ch, v, _sid in commands.strength_calls}
        self.assertIn(("A", 0), last)
        self.assertIn(("B", 0), last)
        self.assertEqual(bridge.engine.signals["phase"], PHASE_COOL)

    def test_output_slot_binding_routes_actions(self):
        commands = _commands(pressure=10.0)
        commands.state.slots["s_out2"] = Slot(slot_id="s_out2", name="负鼠",
                                              type="OVC_010")
        bridge, commands = _bridge({"ramp_s": 0.0, "deny_zap_s": 0.5,
                                    "smooth": 0.0,
                                    "output_slot": "s_out2"}, commands)
        bridge.tick_at(100.0)
        commands.state.slots["s_bmt"].pressure = 45.0
        bridge.tick_at(110.0)
        self.assertTrue(all(sid == "s_out2"
                            for _ch, _sec, sid in commands.zap_calls))

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
        self.assertEqual(bridge.engine.signals["phase"], PHASE_IDLE)
        self.assertEqual(bridge.engine.signals["drive"], 0)
        self.assertEqual(commands.strength_calls, [])

    def test_pause_zeroes_drive(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        bridge.tick_at(100.0)
        self.assertTrue(bridge.toggle_pause())
        bridge.tick_at(110.0)
        self.assertEqual(bridge.engine.signals["drive"], 0)
        self.assertEqual(bridge.engine.signals["phase"], PHASE_IDLE)
        self.assertFalse(bridge.toggle_pause())

    async def test_stop_resets_dispatched_channels_only(self):
        bridge, commands = _bridge({"ramp_s": 0.0},
                                   commands=_commands(pressure=10.0))
        await bridge.start()
        bridge.tick_at(100.0)
        await asyncio.sleep(0)
        await bridge.stop()
        self.assertEqual({ch for ch, _sid in commands.reset_calls},
                         {"A", "B"})

    async def test_stop_without_dispatch_touches_nothing(self):
        bridge, commands = _bridge({"mode": "off"})
        await bridge.start()
        await asyncio.sleep(0)
        await bridge.stop()
        self.assertEqual(commands.reset_calls, [])

    async def test_reload_config_hot_swaps_mappings(self):
        bridge, _ = _bridge()
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "{drive}")
        bridge.config["mappings"] = [
            {"param": "in_strength_a", "expr": "{on_edge} * 100"},
        ]
        await bridge.reload_config()
        self.assertEqual(bridge.engine.mappings.get("in_strength_a"),
                         "{on_edge} * 100")
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
        self.assertEqual(meta["version"], "0.1.0")
        # 七个映射变量与 bridge PARAM_DEFS 一致
        self.assertEqual(set(meta["params"]), set(PARAM_DEFS))
        self.assertEqual(set(meta["params"]),
                         {"pressure", "pressure_pct", "edge", "phase",
                          "drive", "on_edge", "cycles"})
        # 配置声明：玩法 / 设备绑定 / 输入映射表；纯输入模块无输出映射表
        cfg = meta["config"]
        for key in ("mode", "edge_threshold", "recovery_threshold",
                    "cooldown_s", "stim_strength", "cool_strength",
                    "release_strength", "ramp_s", "deny_zap_s",
                    "release_fire_s", "smooth", "sensor_timeout_s",
                    "sensor_slot", "output_slot", "mappings"):
            self.assertIn(key, cfg)
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

    def test_link_params_returns_seven_variables(self):
        module = MarginControlModule()
        params = module.link_params()
        self.assertEqual([name for name, _label in params],
                         ["pressure", "pressure_pct", "edge", "phase",
                          "drive", "on_edge", "cycles"])

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
        self.assertEqual(DEFAULT_MAPPINGS[0]["expr"], "{drive}")
        self.assertEqual(DEFAULT_MAPPINGS[1]["expr"], "{drive}")

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
