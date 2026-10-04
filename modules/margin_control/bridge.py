"""灵猫边控桥接：气压 / 官方边控会话 → 闭环状态机 → 核心映射（唯一设备通道）。

数据流：引擎状态（灵猫槽位的 ``pressure`` 气压 kPa 与 ``edge_state`` 官方
边控状态 0-4，控制页同款语义）→ 指数平滑 → :class:`EdgeGuard` 闭环状态机
（每 0.1s 一拍）→ 九个映射变量喂进模块映射引擎 → **输入映射表**求值派发
设备动作。

**设备控制只经映射表**：状态机不直接调用任何设备命令，只产出三路强度
变量（刺激 / 惩罚 / 助力），由映射表行（默认行
``max({stim_strength}, {punish_strength}, {assist_strength})`` 驱动 A/B
强度）落地；用户可改写行来换目标参数（波形、开火…）或自定义组合。

两种玩法模式（对标官方边控玩法）：

* ``sensor`` 气压闭环：平滑气压升到「边缘阈值」判定到边，立即撤除刺激进入
  冷静期（惩罚时长内以惩罚强度输出）；冷静满「冷静时长」且气压回落到
  「恢复阈值」以下才恢复刺激。循环达「循环上限」后进入释放期（助力强度
  输出），释放满「释放时长」循环计数清零重新开始。
* ``app`` 跟随官方边控会话：DG-Lab 4.0 App（Socket V4）的边控玩法把
  ``edgeState`` 0-4 推给核心——1 刺激 → 维持刺激强度；2/3 冷静计时/判定 →
  撤除刺激（惩罚窗口同 sensor）；4 允许高潮 → 助力强度输出；0 停止 → 待机。
* ``off`` 不闭环，仅提供映射变量。
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

# 九个映射变量（META["params"] 的唯一来源，模块页实时数据区据此展示）；
# 全部设备输出经输入映射表引用这些变量落地，状态机不直接控制设备
PARAM_DEFS: dict[str, dict[str, str]] = {
    "pressure": {"label": "灵猫气压", "desc": "平滑后气压 (kPa)"},
    "pressure_pct": {"label": "气压百分比", "desc": "0-100（60 kPa 满量程，"
                                                  "与控制页曲线一致）"},
    "edge": {"label": "官方边控状态", "desc": "App 边控会话 0-4：0 停止 / "
                                            "1 刺激 / 2 冷静计时 / 3 冷静判定 / 4 允许高潮"},
    "phase": {"label": "闭环阶段", "desc": "0 待机 / 1 刺激 / 2 冷静 / 3 释放"},
    "stim_strength": {"label": "刺激强度", "desc": "刺激期按爬升时长趋向刺激期"
                                                 "强度，冷静期维持冷静强度，其余 0（0-200）"},
    "punish_strength": {"label": "惩罚强度", "desc": "到边进冷静后的惩罚输出，"
                                                   "惩罚时长内非零（0-200）"},
    "assist_strength": {"label": "助力强度", "desc": "释放期输出：App「允许高潮」"
                                                   "或循环上限达成后（0-200）"},
    "on_edge": {"label": "到边标志", "desc": "平滑气压 ≥ 边缘阈值时为 1"},
    "cycles": {"label": "边控循环", "desc": "当前轮「到边→冷静」计数；释放完成"
                                          "后清零重新计"},
}

# 映射表为空时的默认行：三路强度取最大值驱动郊狼/负鼠 A/B 强度——
# 刺激期 = 刺激强度、到边惩罚窗口 = 惩罚强度、释放期 = 助力强度，
# 其余时刻 0；改写行即可换目标参数（in_fire / in_ovc_strength_a …）
DEFAULT_MAPPINGS: list[dict[str, str]] = [
    {"param": "in_strength_a",
     "expr": "max({stim_strength}, {punish_strength}, {assist_strength})"},
    {"param": "in_strength_b",
     "expr": "max({stim_strength}, {punish_strength}, {assist_strength})"},
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
        "ramp_s": 3.0,
        "punish_strength": 100,
        "punish_s": 1.0,
        "assist_strength": 80,
        "release_s": 15.0,
        "cycle_limit": 0,
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

    状态机**不产生任何设备命令**：每拍经 :meth:`outputs` 给出三路强度
    （刺激 / 惩罚 / 助力），由桥接器喂进映射表落地。

    :param config: 模块配置（与 MarginBridge 共享同一字典，改键即热生效）
    :attr phase: 当前阶段（PHASE_*，映射变量 ``phase``）
    :attr cycles: 当前轮「到边→冷静」计数（映射变量 ``cycles``；
                  释放完成后清零重新计）

    :meth:`step` 每拍调用推进状态转移；:meth:`outputs` 返回本拍三路输出
    ``(刺激, 惩罚, 助力)``，均 0-200。
    """

    def __init__(self, config: dict):
        self.config = config
        self.phase = PHASE_IDLE
        self.phase_since = 0.0
        self.cycles = 0
        self.punish_until = 0.0          # 惩罚窗口截止时刻（monotonic）

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
        """归位待机（三路输出 0），不清零循环计数。"""
        self.phase = PHASE_IDLE
        self.phase_since = now
        self.punish_until = 0.0

    def _enter(self, phase: int, now: float) -> None:
        """进入阶段：重复进入无动作；离开释放期清零循环计数（新一轮）；
        进冷静记一次循环并开启惩罚窗口。"""
        if phase == self.phase:
            return
        leaving_release = self.phase == PHASE_RELEASE
        self.phase = phase
        self.phase_since = now
        if leaving_release:
            self.cycles = 0
        if phase == PHASE_COOL:
            self.cycles += 1
            punish_s = self._f("punish_s")
            if punish_s > 0 and self._i("punish_strength") > 0:
                self.punish_until = now + punish_s
            else:
                self.punish_until = 0.0

    def _stim_target(self, now: float) -> int:
        """刺激期目标：按 ``ramp_s`` 从 0 缓升到刺激期强度（挑逗感）。"""
        stim = max(0, self._i("stim_strength", 60))
        ramp = self._f("ramp_s")
        if ramp <= 0:
            return stim
        t = min(1.0, max(0.0, (now - self.phase_since) / ramp))
        return int(round(stim * t))

    def outputs(self, now: float) -> tuple[int, int, int]:
        """本拍三路输出 ``(刺激, 惩罚, 助力)``：

        * 刺激期 → 刺激强度（爬升中）；冷静期 → 冷静强度，惩罚窗口内叠加
          惩罚强度（映射行取最大值即惩罚优先）；释放期 → 助力强度；待机全 0。
        """
        if self.phase == PHASE_STIM:
            return self._stim_target(now), 0, 0
        if self.phase == PHASE_COOL:
            punish = max(0, self._i("punish_strength")) \
                if (self.punish_until > 0 and now < self.punish_until) else 0
            return max(0, self._i("cool_strength")), punish, 0
        if self.phase == PHASE_RELEASE:
            return 0, 0, max(0, self._i("assist_strength"))
        return 0, 0, 0

    def step(self, pressure: float | None, edge: int | None,
             fresh: bool, now: float) -> None:
        """推进一拍状态转移。

        :param pressure: 平滑后气压 (kPa)；``fresh`` 为假时忽略
        :param edge: 官方边控状态 0-4（无会话为 None）
        :param fresh: 气压读数是否在 ``sensor_timeout_s`` 内（失联 fail-safe）
        :param now: monotonic 时间戳
        """
        mode = self._mode()
        if mode == "off" or not fresh:
            # off 模式不闭环；气压失联 fail-safe 归零待机
            self._enter(PHASE_IDLE, now)
            return
        if mode == "app":
            self._step_app(edge, now)
            return
        # sensor：释放计时——release_s=0 保持释放（直到暂停/失联）
        if self.phase == PHASE_RELEASE:
            release_s = self._f("release_s")
            if release_s > 0 and (now - self.phase_since) >= release_s:
                self._enter(PHASE_STIM, now)
        self._step_sensor(pressure, now)

    def _step_app(self, edge: int | None, now: float) -> None:
        """跟随官方边控会话（Socket V4 ``edgeState`` 0-4）：
        1 刺激 → 维持刺激；2/3 冷静计时/判定 → 撤除（惩罚窗口同 sensor）；
        4 允许高潮 → 释放（助力强度）；0 停止 / 无会话 → 待机。
        离开释放（会话开始新一轮）自动清零循环计数。"""
        state = int(edge) if edge is not None else 0
        if state == 1:
            self._enter(PHASE_STIM, now)
        elif state in (2, 3):
            self._enter(PHASE_COOL, now)
        elif state >= 4:
            self._enter(PHASE_RELEASE, now)
        else:
            self._enter(PHASE_IDLE, now)

    def _step_sensor(self, pressure: float | None, now: float) -> None:
        """气压闭环：升到边缘阈值判定到边（进冷静），冷静满最短时长且
        气压回落到恢复阈值以下才恢复刺激——两阈值构成回差防抖。
        循环达「循环上限」后改为进入释放期（助力输出），释放满
        「释放时长」清零计数重新开始。"""
        if pressure is None:
            self._enter(PHASE_IDLE, now)
            return
        threshold = self._f("edge_threshold", 40.0)
        recovery = self._f("recovery_threshold", 20.0)
        cooldown = self._f("cooldown_s", 10.0)
        if self.phase == PHASE_IDLE:
            self._enter(PHASE_STIM, now)
            return
        if self.phase == PHASE_RELEASE:
            return                       # 由释放计时 / 失联 / 暂停退出
        if self.phase == PHASE_STIM and pressure >= threshold:
            self._enter(PHASE_COOL, now)
            return
        if self.phase == PHASE_COOL:
            waited = (now - self.phase_since) >= cooldown
            if waited and pressure <= recovery:
                limit = max(0, self._i("cycle_limit"))
                if 0 < limit <= self.cycles:
                    self._enter(PHASE_RELEASE, now)
                else:
                    self._enter(PHASE_STIM, now)


# ---------------------------------------------------------------- 桥接器

class MarginBridge:
    """灵猫边控运行时：气压/边控状态读取 + 闭环状态机 + 映射表派发。

    :param config: 模块配置（MarginConfig，reload_config 时原位更新）
    :param get_state: 引擎状态回调（``ctx.engine.get_state``）
    :param commands: 引擎命令层（``ctx.engine``；仅经映射派发器间接使用，
                     模块不直接调用任何设备命令）
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
        self._last_phase: int = PHASE_IDLE
        self.last_values: dict[str, float] = {}

        # 映射引擎：信号空间 = 边控变量 ∪ 核心输出参数实时值；
        # 设备动作只由输入映射表求值派发（build_dispatchers 派发器）
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
        """把引擎命令层适配成核心参数派发器需要的接口（仅映射表派发用）。"""

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

    # ---- 设备定位（映射表派发的目标绑定） --------------------------------

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
        # 经映射表归零：状态机归位待机并把三路输出清零重新求值——
        # 映射行结果变化（如刺激 60→0）即派发归零；表无强度行则不动设备
        self.guard.reset(self._clock())
        self.engine.signal("phase", self.guard.phase)
        self.engine.signal("stim_strength", 0)
        self.engine.signal("punish_strength", 0)
        self.engine.signal("assist_strength", 0)
        self._refresh_last_values()
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
        """暂停/恢复闭环（负鼠按键动作）：暂停即状态机归位，下一拍三路
        输出清零经映射表派发归零，恢复后重新爬升。"""
        self.paused = not self.paused
        self.log("边控闭环已暂停（输出经映射表归零）" if self.paused
                 else "边控闭环已恢复")
        return self.paused

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
            self.guard.reset(now)
        else:
            self.guard.step(pressure if fresh else None, edge, fresh, now)
        self._log_offline(fresh, now)
        self._log_phase()

        stim, punish, assist = self.guard.outputs(now)
        on_edge = 1 if (pressure is not None and fresh
                        and pressure >= threshold) else 0
        pct = max(0.0, min(100.0, (pressure or 0.0)
                           / PRESSURE_MAX_KPA * 100.0))
        self.engine.signal("pressure", round(pressure or 0.0, 2))
        self.engine.signal("pressure_pct", round(pct, 1))
        self.engine.signal("edge", int(edge) if edge is not None else 0)
        self.engine.signal("phase", self.guard.phase)
        self.engine.signal("stim_strength", stim)
        self.engine.signal("punish_strength", punish)
        self.engine.signal("assist_strength", assist)
        self.engine.signal("on_edge", on_edge)
        self.engine.signal("cycles", self.guard.cycles)
        self._refresh_last_values()

    def _refresh_last_values(self) -> None:
        self.last_values = {name: float(self.engine.signals.get(name, 0.0) or 0.0)
                            for name in PARAM_DEFS}

    def _log_phase(self) -> None:
        """阶段切换提示（每次变化一条，含当前循环计数）。"""
        if self.guard.phase == self._last_phase:
            return
        prev, self._last_phase = self._last_phase, self.guard.phase
        if prev == PHASE_IDLE and self.guard.phase == PHASE_IDLE:
            return
        self.log(f"闭环阶段 → {PHASE_LABELS.get(self.guard.phase, self.guard.phase)}"
                 f"（循环 {self.guard.cycles}）")

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
