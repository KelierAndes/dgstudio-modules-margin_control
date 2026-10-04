"""灵猫边控桥接：气压 / 官方边控会话 → 闭环状态机 → 核心映射与派发。

数据流：引擎状态（灵猫槽位的 ``pressure`` 气压 kPa 与 ``edge_state`` 官方
边控状态 0-4，控制页同款语义）→ 指数平滑 → :class:`EdgeGuard` 闭环状态机
（每 0.1s 一拍）→ 七个映射变量喂进模块映射引擎 → 输入映射表求值派发设备
动作；默认行「{drive} 驱动 A/B 强度」安装即可用，表可完全自定义。

两种玩法模式（对标官方边控玩法）：

* ``sensor`` 气压闭环：平滑气压升到「边缘阈值」判定到边，立即撤除刺激进入
  冷静期；冷静满「冷静时长」且气压回落到「恢复阈值」以下才恢复刺激，往复
  循环（到边可选拿双通道惩罚脉冲）。气压失联 ``sensor_timeout_s`` 判定
  fail-safe 归零待机。
* ``app`` 跟随官方边控会话：DG-Lab 4.0 App（Socket V4）的边控玩法把
  ``edgeState`` 0-4 推给核心——1 刺激 → 维持刺激强度；2/3 冷静计时/判定 →
  撤除刺激；4 允许高潮 → 释放强度（可选定时开火助飞）；0 停止 → 待机。

映射关系全部落在核心统一的输入映射表上（配置项 ``mappings``），表为空时
按默认行落地。
"""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, Callable

from dglab.mapping import MappingEngine
from dglab.params import (build_dispatchers, core_alias_values, core_inputs,
                          device_state_values, input_ranges)
from dglab.state import family_of

__all__ = ["MarginBridge", "MarginConfig", "EdgeGuard", "PARAM_DEFS",
           "DEFAULT_MAPPINGS", "PRESSURE_MAX_KPA",
           "PHASE_IDLE", "PHASE_STIM", "PHASE_COOL", "PHASE_RELEASE",
           "PHASE_LABELS"]

# 闭环节拍：0.1s 一拍（与核心脉冲帧 / 音频联动的节奏一致）
TICK_S = 0.1
# 气压满量程 (kPa)：与控制页灵猫曲线一致（ui/live.PRESSURE_MAX_KPA）
PRESSURE_MAX_KPA = 60.0

# 闭环阶段（映射变量 ``phase``）
PHASE_IDLE = 0
PHASE_STIM = 1
PHASE_COOL = 2
PHASE_RELEASE = 3
PHASE_LABELS = {PHASE_IDLE: "待机", PHASE_STIM: "刺激",
                PHASE_COOL: "冷静", PHASE_RELEASE: "释放"}

# 七个映射变量（META["params"] 的唯一来源，模块页实时数据区据此展示）
PARAM_DEFS: dict[str, dict[str, str]] = {
    "pressure": {"label": "灵猫气压", "desc": "平滑后气压 (kPa)"},
    "pressure_pct": {"label": "气压百分比", "desc": "0-100（60 kPa 满量程，"
                                                  "与控制页曲线一致）"},
    "edge": {"label": "官方边控状态", "desc": "App 边控会话 0-4：0 停止 / "
                                            "1 刺激 / 2 冷静计时 / 3 冷静判定 / 4 允许高潮"},
    "phase": {"label": "闭环阶段", "desc": "0 待机 / 1 刺激 / 2 冷静 / 3 释放"},
    "drive": {"label": "目标强度", "desc": "状态机当前目标强度 0-200"
                                          "（默认行直接驱动 A/B 强度）"},
    "on_edge": {"label": "到边标志", "desc": "平滑气压 ≥ 边缘阈值时为 1"},
    "cycles": {"label": "边控循环", "desc": "本次运行累计「到边→冷静」次数"},
}

# 映射表为空时的默认行：状态机目标强度直接驱动郊狼/负鼠 A/B 强度
DEFAULT_MAPPINGS: list[dict[str, str]] = [
    {"param": "in_strength_a", "expr": "{drive}"},
    {"param": "in_strength_b", "expr": "{drive}"},
]


class MarginConfig(dict):
    """边控模块配置：缺省值优先取模块声明（defaults 参数），DEFAULTS 为兜底。"""

    DEFAULTS = {
        "mode": "sensor",
        "edge_threshold": 40.0,
        "recovery_threshold": 20.0,
        "cooldown_s": 10.0,
        "stim_strength": 60,
        "cool_strength": 0,
        "release_strength": 0,
        "ramp_s": 3.0,
        "deny_zap_s": 0.0,
        "release_fire_s": 0.0,
        "smooth": 0.5,
        "sensor_timeout_s": 5.0,
        "sensor_slot": "",
        "output_slot": "",
        "mappings": [],
    }

    def __init__(self, data: dict | None = None, defaults: dict | None = None):
        super().__init__({k: v for k, v in
                          (defaults or self.DEFAULTS).items()})
        if data:
            self.update({k: v for k, v in data.items() if v is not None})


# ---------------------------------------------------------------- 闭环状态机

class EdgeGuard:
    """边控闭环状态机（纯逻辑，不碰 asyncio / 引擎，便于单测）。

    :param config: 模块配置（与 MarginBridge 共享同一字典，改键即热生效）
    :attr phase: 当前阶段（PHASE_*，映射变量 ``phase``）
    :attr drive: 当前目标强度 0-200（映射变量 ``drive``）
    :attr cycles: 累计「到边→冷静」次数（映射变量 ``cycles``）

    :meth:`step` 每拍调用，返回本拍触发的设备动作列表
    ``[("zap", seconds), ("fire", seconds)]``，由桥接器落到目标输出设备。
    """

    def __init__(self, config: dict):
        self.config = config
        self.phase = PHASE_IDLE
        self.phase_since = 0.0
        self.drive = 0
        self.cycles = 0
        self.last_actions: list[tuple[str, float]] = []

    # ---- 配置快捷读取（每拍读，改配置即热生效） -------------------------

    def _mode(self) -> str:
        return str(self.config.get("mode") or "sensor")

    def _f(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.config.get(key))
        except (TypeError, ValueError):
            return default

    def _i(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.config.get(key)))
        except (TypeError, ValueError):
            return default

    # ---- 状态转移 -------------------------------------------------------

    def reset(self, now: float = 0.0) -> None:
        """归位待机（目标强度 0），不清零循环计数。"""
        self._enter(PHASE_IDLE, now)
        self.drive = 0

    def _enter(self, phase: int, now: float) -> list[tuple[str, float]]:
        """进入阶段：重复进入无动作；到边进冷静记一次循环并可选拿惩罚
        脉冲，进释放可选定时开火。"""
        actions: list[tuple[str, float]] = []
        if phase != self.phase:
            self.phase = phase
            self.phase_since = now
            if phase == PHASE_COOL:
                self.cycles += 1
                zap_s = self._f("deny_zap_s")
                if zap_s > 0:
                    actions.append(("zap", zap_s))
            elif phase == PHASE_RELEASE:
                fire_s = self._f("release_fire_s")
                if fire_s > 0:
                    actions.append(("fire", fire_s))
        return actions

    def _drive_target(self, now: float) -> int:
        """当前阶段的目标强度：刺激期按 ``ramp_s`` 从 0 缓升（挑逗感），
        冷静/释放期维持设定值，待机为 0。"""
        if self.phase == PHASE_STIM:
            stim = max(0, self._i("stim_strength", 60))
            ramp = self._f("ramp_s")
            if ramp <= 0:
                return stim
            t = min(1.0, max(0.0, (now - self.phase_since) / ramp))
            return int(round(stim * t))
        if self.phase == PHASE_COOL:
            return max(0, self._i("cool_strength", 0))
        if self.phase == PHASE_RELEASE:
            return max(0, self._i("release_strength", 0))
        return 0

    def step(self, pressure: float | None, edge: int | None,
             fresh: bool, now: float) -> list[tuple[str, float]]:
        """推进一拍。

        :param pressure: 平滑后气压 (kPa)；``fresh`` 为假时忽略
        :param edge: 官方边控状态 0-4（无会话为 None）
        :param fresh: 气压读数是否在 ``sensor_timeout_s`` 内（失联 fail-safe）
        :param now: monotonic 时间戳
        """
        self.last_actions = []
        mode = self._mode()
        if mode == "off" or not fresh:
            # off 模式不闭环；气压失联 fail-safe 归零待机
            self._enter(PHASE_IDLE, now)
            self.drive = 0
            return self.last_actions
        if mode == "app":
            self.last_actions = self._step_app(edge, now)
        else:
            self.last_actions = self._step_sensor(pressure, now)
        self.drive = self._drive_target(now)
        return self.last_actions

    def _step_app(self, edge: int | None,
                  now: float) -> list[tuple[str, float]]:
        """跟随官方边控会话（Socket V4 ``edgeState`` 0-4）：
        1 刺激 → 维持刺激；2/3 冷静计时/判定 → 撤除；4 允许高潮 → 释放；
        0 停止 / 无会话 → 待机。"""
        state = int(edge) if edge is not None else 0
        if state == 1:
            return self._enter(PHASE_STIM, now)
        if state in (2, 3):
            return self._enter(PHASE_COOL, now)
        if state >= 4:
            return self._enter(PHASE_RELEASE, now)
        return self._enter(PHASE_IDLE, now)

    def _step_sensor(self, pressure: float | None,
                     now: float) -> list[tuple[str, float]]:
        """气压闭环：升到边缘阈值判定到边（进冷静），冷静满最短时长且
        气压回落到恢复阈值以下才恢复刺激——两阈值构成回差防抖。"""
        if pressure is None:
            return self._enter(PHASE_IDLE, now)
        threshold = self._f("edge_threshold", 40.0)
        recovery = self._f("recovery_threshold", 20.0)
        cooldown = self._f("cooldown_s", 10.0)
        if self.phase in (PHASE_IDLE, PHASE_RELEASE):
            return self._enter(PHASE_STIM, now)
        if self.phase == PHASE_STIM and pressure >= threshold:
            return self._enter(PHASE_COOL, now)
        if self.phase == PHASE_COOL:
            waited = (now - self.phase_since) >= cooldown
            if waited and pressure <= recovery:
                return self._enter(PHASE_STIM, now)
        return []


# ---------------------------------------------------------------- 桥接器

class MarginBridge:
    """灵猫边控运行时：气压/边控状态读取 + 闭环状态机 + 映射派发。

    :param config: 模块配置（MarginConfig，reload_config 时原位更新）
    :param get_state: 引擎状态回调（``ctx.engine.get_state``）
    :param commands: 引擎命令层（``ctx.engine``，需要 resolve_slot /
                     set_strength / reset_strength / zap / fire 等）
    :param events: 应用事件总线（保留，当前无订阅）
    """

    def __init__(self, config: MarginConfig, get_state: Callable[[], Any],
                 commands: Any, events=None):
        self.config = config
        self.get_state = get_state
        self.commands = commands

        self.log: Callable[[str], None] = print
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._clock: Callable[[], float] = time.monotonic   # 测试可注入

        self.paused = False
        self.guard = EdgeGuard(config)
        self._smoothed: float | None = None
        self._last_pressure_at: float | None = None
        self._last_offline_log = float("-inf")
        self.last_values: dict[str, float] = {}
        self.last_actions: list[tuple[str, float]] = []

        # 映射引擎：信号空间 = 边控变量 ∪ 核心输出参数实时值
        self.engine = MappingEngine(self._dispatch,
                                    device_vars=self._device_vars,
                                    ranges=input_ranges())
        self._api = self._DeviceApi(self)
        self.dispatchers = build_dispatchers(self._api, core_inputs())
        self._primed = False
        self.apply_config()

    # ---- 映射表 ---------------------------------------------------------

    def apply_config(self) -> None:
        """装载映射表；首轮只静默求值，避免启动即把设备写成 0。"""
        first = not self._primed
        if first:
            self.engine.armed = False
        self.engine.set_mappings(self._effective_rows())
        if first:
            self.engine.armed = True
            self._primed = True

    def _effective_rows(self) -> list[dict]:
        rows = [row for row in (self.config.get("mappings") or [])
                if isinstance(row, dict)
                and str(row.get("param") or "").strip()]
        return rows or [dict(row) for row in DEFAULT_MAPPINGS]

    def _safe_state(self):
        try:
            return self.get_state()
        except Exception:
            return None

    def _device_vars(self) -> dict[str, float]:
        """表达式可用的核心输出参数实时值 + 短名别名。"""
        vals = device_state_values(self._safe_state())
        vals.update(core_alias_values(vals))
        return vals

    def _dispatch(self, target: str, value: int) -> None:
        runner = self.dispatchers.get(target)
        if runner is None:
            return
        try:
            runner(value)
        except Exception as exc:
            self.log(f"映射派发 {target}={value} 失败: {exc!r}")

    class _DeviceApi:
        """把引擎命令层适配成核心参数派发器需要的接口。"""

        def __init__(self, bridge: "MarginBridge"):
            self._b = bridge

        @property
        def _cmd(self):
            return self._b.commands

        def resolve_slot(self, family: str = "") -> str | None:
            state = self._b._safe_state()
            if state is None:
                return None
            # 用户绑定了目标输出设备且家族匹配时优先
            explicit = self._b._explicit_output_slot(state)
            if explicit is not None and (
                    not family
                    or family_of(state.slots[explicit].type) == family):
                return explicit
            slots = {sid: state.slots[sid] for sid in sorted(state.slots)}
            if family:
                for sid, slot in slots.items():
                    if family_of(slot.type) == family:
                        return sid
            for sid, slot in slots.items():
                if family_of(slot.type) != "BMTR":
                    return sid
            return next(iter(slots), None)

        def wave_order(self, family: str = "") -> list[str]:
            from dglab.waves import wave_order
            return wave_order(family or "COYOTE")

        def wave_selection(self) -> dict:
            getter = getattr(self._cmd, "wave_selection", None)
            return (getter() or {}) if getter is not None else {}

        def set_strength(self, channel, value, slot_id=None):
            return self._cmd.set_strength(channel, value, slot_id=slot_id)

        def set_wave(self, channel, name, slot_id=None):
            return self._cmd.set_wave(channel, name, slot_id=slot_id)

        def zap(self, channel, seconds=1.0, slot_id=None):
            return self._cmd.zap(channel, seconds, slot_id=slot_id)

        def fire_start(self, slot_id=None, channel=None):
            return self._cmd.fire_start(slot_id=slot_id, channel=channel)

        def fire_stop(self, slot_id=None, channel=None):
            return self._cmd.fire_stop(slot_id=slot_id, channel=channel)

        def emergency_stop(self):
            return self._cmd.emergency_stop()

        def run(self, coro) -> None:
            self._b._spawn(coro)

    # ---- 设备定位 -------------------------------------------------------

    def _sensor_slot(self, state):
        """绑定的灵猫槽位：配置 slot_id 优先，否则第一台 BMTR。"""
        if state is None:
            return None
        want = str(self.config.get("sensor_slot") or "").strip()
        if want and want in state.slots:
            return state.slots[want]
        for sid in sorted(state.slots):
            if family_of(state.slots[sid].type) == "BMTR":
                return state.slots[sid]
        return None

    def _explicit_output_slot(self, state) -> str | None:
        """配置绑定的目标输出设备（存在且确为输出设备才生效）。"""
        want = str(self.config.get("output_slot") or "").strip()
        if want and want in (state.slots or {}) \
                and state.slots[want].is_output_device:
            return want
        return None

    def _output_slot(self) -> str | None:
        """惩罚脉冲 / 释放开火 / 停止归零的目标输出设备。"""
        state = self._safe_state()
        if state is not None:
            explicit = self._explicit_output_slot(state)
            if explicit:
                return explicit
        try:
            return self.commands.resolve_slot(output_only=True)
        except Exception:
            return None

    # ---- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._tick_loop())
        mode = str(self.config.get("mode") or "sensor")
        self.log(f"灵猫边控联动已启动（模式 {mode}，每 {TICK_S:g}s 一拍；"
                 f"边缘阈值 {self.config.get('edge_threshold')} kPa）")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        await self._release_dispatched()
        self.log("灵猫边控联动已停止")

    def close(self) -> None:
        """模块卸载清理（宿主先经 stop() 归零，这里兜底停拍）。"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def reload_config(self) -> None:
        """联动页保存设置后由宿主调用：映射表与闭环参数热生效。"""
        self.apply_config()

    def toggle_pause(self) -> bool:
        """暂停/恢复闭环（负鼠按键动作）：暂停即待机归零，恢复重新爬升。"""
        self.paused = not self.paused
        self.log("边控闭环已暂停（目标强度归零）" if self.paused
                 else "边控闭环已恢复")
        return self.paused

    async def _release_dispatched(self) -> None:
        """停止时归零本模块实际派发过的强度通道（自定义表派发了哪些就
        归零哪些；未派发过则不动设备，避免误清手动强度）。"""
        for ch in ("A", "B"):
            dispatched = int(self.engine.last_values.get(
                f"in_strength_{ch.lower()}", 0) or 0)
            if dispatched <= 0:
                continue
            sid = self._output_slot()
            if sid is None:
                continue
            try:
                await self.commands.reset_strength(ch, slot_id=sid)
                self.log(f"停止归零：通道 {ch}（此前派发强度 {dispatched}）")
            except Exception as exc:
                self.log(f"停止归零通道 {ch} 失败: {exc!r}")

    # ---- 节拍与状态机推进 -------------------------------------------------

    async def _tick_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(TICK_S)
                try:
                    self._tick()
                except Exception:
                    self._log_error("边控节拍失败")
        except asyncio.CancelledError:
            pass

    def _tick(self) -> None:
        cfg = self.config
        state = self._safe_state()
        slot = self._sensor_slot(state)
        raw = slot.pressure if slot is not None else None
        edge = slot.edge_state if slot is not None else None
        now = self._clock()

        timeout = max(1.0, self._cfg_f(cfg, "sensor_timeout_s", 5.0))
        if raw is not None:
            self._last_pressure_at = now
        fresh = (self._last_pressure_at is not None
                 and now - self._last_pressure_at <= timeout)

        smooth = min(0.95, max(0.0, self._cfg_f(cfg, "smooth", 0.5)))
        if raw is not None:
            if self._smoothed is None or smooth <= 0:
                self._smoothed = float(raw)
            else:
                self._smoothed += (1.0 - smooth) * (float(raw) - self._smoothed)
        pressure = self._smoothed

        threshold = self._cfg_f(cfg, "edge_threshold", 40.0)
        if self.paused:
            actions: list[tuple[str, float]] = []
            self.guard.reset(now)
        else:
            actions = self.guard.step(
                pressure if fresh else None, edge, fresh, now)
        self.last_actions = actions

        self._log_offline(fresh, now)

        on_edge = 1 if (pressure is not None and fresh
                        and pressure >= threshold) else 0
        pct = max(0.0, min(100.0, (pressure or 0.0)
                           / PRESSURE_MAX_KPA * 100.0))
        self.engine.signal("pressure", round(pressure or 0.0, 2))
        self.engine.signal("pressure_pct", round(pct, 1))
        self.engine.signal("edge", int(edge) if edge is not None else 0)
        self.engine.signal("phase", self.guard.phase)
        self.engine.signal("drive", int(self.guard.drive))
        self.engine.signal("on_edge", on_edge)
        self.engine.signal("cycles", self.guard.cycles)
        self.last_values = {name: float(self.engine.signals.get(name, 0.0) or 0.0)
                            for name in PARAM_DEFS}
        self._run_actions(actions)

        if actions:
            self.log(f"阶段 → {PHASE_LABELS.get(self.guard.phase, self.guard.phase)}"
                     f"（目标强度 {self.guard.drive}，累计 {self.guard.cycles} 次）")

    @staticmethod
    def _cfg_f(cfg: dict, key: str, default: float) -> float:
        try:
            return float(cfg.get(key))
        except (TypeError, ValueError):
            return default

    def _log_offline(self, fresh: bool, now: float) -> None:
        """气压失联提示：每 30s 至多一条（无灵猫时提示绑定缺失）。"""
        mode = str(self.config.get("mode") or "sensor")
        if self.paused or mode == "off" or fresh:
            return
        if now - self._last_offline_log < 30.0:
            return
        self._last_offline_log = now
        if self._last_pressure_at is None:
            self.log("未发现灵猫气压读数（确认灵猫已连接，"
                     "或「灵猫设备」绑定正确）；闭环待机中")
        else:
            self.log("灵猫气压停止更新（可能失联），闭环归零待机中")

    def _run_actions(self, actions: list[tuple[str, float]]) -> None:
        for kind, seconds in actions:
            sid = self._output_slot()
            if sid is None:
                self.log(f"边控动作 {kind} 无输出设备可执行")
                continue
            if kind == "zap":
                for ch in ("A", "B"):
                    self._spawn(self.commands.zap(ch, float(seconds),
                                                  slot_id=sid))
            elif kind == "fire":
                self._spawn(self.commands.fire(slot_id=sid,
                                               duration_s=float(seconds)))

    def _spawn(self, coro) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)

    def _log_error(self, prefix: str) -> None:
        self.log(f"{prefix}:\n{traceback.format_exc()}")
