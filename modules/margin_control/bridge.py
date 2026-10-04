"""灵猫边控桥接：气压 / 官方边控会话 → 闭环状态机 → 核心映射（唯一设备通道）。

数据流：引擎状态（灵猫槽位的 ``pressure`` 气压 kPa 与 ``edge_state`` 官方
边控状态 0-4，控制页同款语义）→ 指数平滑 + 漏气补偿 → :class:`EdgeGuard`
闭环状态机（每 0.1s 一拍，判定条件与阈值自适应对标 DG-Lab 官方边控玩法
设置页）→ 七个映射变量喂进模块映射引擎 → **输入映射表**求值派发设备动作。

**设备控制只经映射表**：状态机不直接调用任何设备命令，只产出两路强度
变量（刺激器 / 惩罚器），由映射表行（默认行
``max({stim_strength}, {punish_strength})`` 驱动 A/B 强度）落地。

sensor 模式判定条件（对标官方「判定条件」页）：

* 红线（边缘气压阈值）：气压高于红线判即将高潮；可要求**持续高于** N 秒；
* 气压跳变：短窗口内上升速率达 N kPa/s 也判即将高潮（官方「气压短时间
  上升差值」）；
* 蓝线（恢复气压阈值）：冷静期满最小冷静时间且气压低于蓝线判恢复；
  可要求**持续低于** N 秒；
* 阈值自适应（官方「阈值自适应调整」）：刺激阶段每次成功边控后红线按
  百分比下降、超时未到边红线缓慢下降，蓝线按比例跟随；冷静阶段超时未
  恢复蓝线缓慢上升（恢复变容易）；红线/蓝线间保持安全间隙。

释放触发（满足其一即进释放期，助力强度经刺激器变量输出）：

* 边控次数：指定边控循环轮数后（官方「固定模式」）；
* 持续时长：游戏进行指定时长后（官方「游戏进行指定时长后，允许高潮释放」）；
* app 模式：跟随官方会话状态 4（允许高潮），次数/时长不介入。

``off`` 模式不闭环，仅提供映射变量。
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
           "DEFAULT_MAPPINGS",
           "PHASE_IDLE", "PHASE_STIM", "PHASE_COOL", "PHASE_RELEASE",
           "PHASE_LABELS"]

# 闭环节拍：0.1s 一拍（与核心脉冲帧 / 音频联动的节奏一致）
TICK_S = 0.1
# 单拍最大按秒推进量：暂停/失联恢复后自适应不瞬移
MAX_STEP_DT = 5.0
# 红蓝线安全间隙 (kPa)：蓝线永不超过红线 - GAP
THRESHOLD_GAP = 0.5

# 闭环阶段（内部状态；对外经 on_release 等变量表达）
PHASE_IDLE = 0
PHASE_STIM = 1
PHASE_COOL = 2
PHASE_RELEASE = 3
PHASE_LABELS = {PHASE_IDLE: "待机", PHASE_STIM: "刺激",
                PHASE_COOL: "冷静", PHASE_RELEASE: "释放"}

# 七个映射变量（META["params"] 的唯一来源，模块页实时数据区据此展示）；
# 全部设备输出经输入映射表引用这些变量落地，状态机不直接控制设备
PARAM_DEFS: dict[str, dict[str, str]] = {
    "pressure": {"label": "灵猫气压", "desc": "平滑 + 漏气补偿后气压 (kPa)"},
    "edge": {"label": "官方边控状态", "desc": "App 边控会话 0-4：0 停止 / "
                                            "1 刺激 / 2 冷静计时 / 3 冷静判定 / 4 允许高潮"},
    "stim_strength": {"label": "刺激器强度", "desc": "刺激期按爬升时长趋向刺激"
                                                   "强度，冷静期维持冷静强度，释放期输出"
                                                   "助力强度，其余 0（0-200）"},
    "punish_strength": {"label": "惩罚器强度", "desc": "到边进冷静后的惩罚输出，"
                                                     "惩罚时长内非零（0-200）"},
    "on_edge": {"label": "到边标志", "desc": "气压 ≥ 当前边缘阈值（自适应后"
                                            "红线）时为 1"},
    "on_release": {"label": "释放标志", "desc": "释放期（允许高潮，助力输出）"
                                              "为 1，其余 0"},
    "cycles": {"label": "边控循环", "desc": "当前轮「到边→冷静」计数；释放完成"
                                          "后清零重新计"},
}

# 映射表为空时的默认行：刺激器/惩罚器取最大值驱动郊狼/负鼠 A/B 强度——
# 刺激期与释放期 = 刺激器强度、到边惩罚窗口 = 惩罚器强度，其余时刻 0；
# 改写行即可换目标参数（in_fire / in_ovc_strength_a …）
DEFAULT_MAPPINGS: list[dict[str, str]] = [
    {"param": "in_strength_a",
     "expr": "max({stim_strength}, {punish_strength})"},
    {"param": "in_strength_b",
     "expr": "max({stim_strength}, {punish_strength})"},
]


class MarginConfig(dict):
    """边控模块配置：缺省值优先取模块声明（defaults 参数），DEFAULTS 为兜底。"""

    DEFAULTS = {
        "mode": "sensor",
        "sensor_slot": "",
        "smooth": 0.5,
        "sensor_timeout_s": 5.0,
        # 判定条件
        "edge_threshold": 17.0,
        "recovery_threshold": 15.0,
        "edge_hold_s": 0.0,
        "jump_rise": 0.0,
        "jump_window_s": 2.0,
        "cooldown_s": 10.0,
        "recovery_hold_s": 5.0,
        # 释放
        "cycle_limit": 5,
        "time_release_s": 0.0,
        "release_s": 15.0,
        # 强度
        "stim_strength": 60,
        "cool_strength": 0,
        "assist_strength": 80,
        "ramp_s": 3.0,
        "punish_strength": 100,
        "punish_s": 1.0,
        # 阈值自适应
        "adapt_stim": True,
        "adapt_drop_pct": 0.0,
        "adapt_drop_delay_s": 100.0,
        "adapt_drop_rate": 1.0,
        "adapt_blue_follow": 30.0,
        "adapt_cool": True,
        "adapt_blue_rise_rate": 0.5,
        "adapt_blue_delay_s": 100.0,
        "leak_comp": 0.0,
        # 映射表
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

    判定条件与阈值自适应对标 DG-Lab 官方边控玩法设置页；状态机**不产生
    任何设备命令**：每拍经 :meth:`outputs` 给出两路强度（刺激器 / 惩罚器），
    由桥接器喂进映射表落地。

    :param config: 模块配置（与 MarginBridge 共享同一字典，改键即热生效）
    :attr phase: 当前阶段（PHASE_*，内部状态；对外经 on_release 等表达）
    :attr cycles: 当前轮「到边→冷静」计数（映射变量 ``cycles``）
    :attr red_drop / blue_shift: 阈值自适应累计量 (kPa)

    :meth:`step` 每拍调用推进状态转移；:meth:`outputs` 返回本拍两路输出
    ``(刺激器, 惩罚器)``；:meth:`red_threshold` / :meth:`blue_threshold`
    返回自适应后的当前红线/蓝线。
    """

    def __init__(self, config: dict):
        self.config = config
        self.phase = PHASE_IDLE
        self.phase_since = 0.0
        self.cycles = 0
        self.punish_until = 0.0          # 惩罚窗口截止时刻（monotonic）
        self.session_since: float | None = None   # 会话开始（首次进刺激）
        # 阈值自适应累计量：红线下降量（≥0）、蓝线偏移（负=跟随下降，正=上升）
        self.red_drop = 0.0
        self.blue_shift = 0.0
        # 判定辅助状态
        self._above_since: float | None = None    # 持续高于红线起点
        self._below_since: float | None = None    # 持续低于蓝线起点
        self._history: list[tuple[float, float]] = []   # 跳变窗口 (now, p)
        self._last_now: float | None = None

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

    def _b(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        return bool(value) if not isinstance(value, str) \
            else value.lower() in ("1", "true", "yes", "on")

    # ---- 自适应阈值 -----------------------------------------------------

    def red_threshold(self) -> float:
        """当前红线（边缘阈值）：配置值 − 自适应累计下降量。"""
        return max(1.0, self._f("edge_threshold", 17.0) - self.red_drop)

    def blue_threshold(self) -> float:
        """当前蓝线（恢复阈值）：配置值 + 自适应偏移，恒低于红线至少
        :data:`THRESHOLD_GAP`。"""
        blue = self._f("recovery_threshold", 15.0) + self.blue_shift
        return min(max(0.0, blue), self.red_threshold() - THRESHOLD_GAP)

    def _drop_red(self, amount: float) -> None:
        """红线下降 amount kPa，蓝线按「跟随下降百分比」同步下探。"""
        if amount <= 0:
            return
        self.red_drop += amount
        follow = self._f("adapt_blue_follow")
        if follow > 0:
            self.blue_shift -= amount * follow / 100.0

    def _adapt_on_edge(self, now: float) -> None:
        """成功边控瞬间的红线自适应：按百分比立即下降。"""
        if not self._b("adapt_stim", True):
            return
        pct = self._f("adapt_drop_pct")
        if pct > 0:
            self._drop_red(self.red_threshold() * pct / 100.0)

    def _adapt_step(self, now: float, dt: float) -> None:
        """逐拍自适应：刺激阶段超时未到边红线缓慢下降；冷静阶段超时未
        恢复蓝线缓慢上升（恢复变容易）。"""
        if dt <= 0:
            return
        if self.phase == PHASE_STIM and self._b("adapt_stim", True):
            delay = self._f("adapt_drop_delay_s")
            rate = self._f("adapt_drop_rate")
            if rate > 0 and (now - self.phase_since) >= delay:
                self._drop_red(self.red_threshold() * rate / 100.0 * dt)
        elif self.phase == PHASE_COOL and self._b("adapt_cool", True):
            delay = self._f("adapt_blue_delay_s")
            rate = self._f("adapt_blue_rise_rate")
            if rate > 0 and (now - self.phase_since) >= delay:
                self.blue_shift += self.blue_threshold() * rate / 100.0 * dt

    def _rise_rate(self, now: float) -> float:
        """跳变窗口内的气压上升速率 (kPa/s)；样本不足返回 0。"""
        window = max(0.2, self._f("jump_window_s", 2.0))
        while self._history and now - self._history[0][0] > window:
            self._history.pop(0)
        if len(self._history) < 2:
            return 0.0
        t0, p0 = self._history[0]
        span = now - t0
        if span < 0.2:
            return 0.0
        return (self._history[-1][1] - p0) / span

    # ---- 状态转移 -------------------------------------------------------

    def reset(self, now: float = 0.0) -> None:
        """归位待机（两路输出 0），保留会话时长与自适应累计。"""
        self.phase = PHASE_IDLE
        self.phase_since = now
        self.punish_until = 0.0
        self._above_since = None
        self._below_since = None
        self._history.clear()

    def _enter(self, phase: int, now: float) -> None:
        """进入阶段：重复进入无动作；离开释放期清零循环计数（新一轮）；
        进冷静记一次循环、开惩罚窗口并做边控后红线自适应；首次进刺激记
        会话开始（持续时长释放的计时起点）。"""
        if phase == self.phase:
            return
        leaving_release = self.phase == PHASE_RELEASE
        self.phase = phase
        self.phase_since = now
        self._above_since = None
        self._below_since = None
        if leaving_release:
            self.cycles = 0
        if phase == PHASE_COOL:
            self.cycles += 1
            punish_s = self._f("punish_s")
            if punish_s > 0 and self._i("punish_strength") > 0:
                self.punish_until = now + punish_s
            else:
                self.punish_until = 0.0
            self._adapt_on_edge(now)
        elif phase == PHASE_STIM and self.session_since is None:
            self.session_since = now

    def _stim_target(self, now: float) -> int:
        """刺激器目标：刺激期按 ``ramp_s`` 从 0 缓升到刺激强度。"""
        stim = max(0, self._i("stim_strength", 60))
        ramp = self._f("ramp_s")
        if ramp <= 0:
            return stim
        t = min(1.0, max(0.0, (now - self.phase_since) / ramp))
        return int(round(stim * t))

    def outputs(self, now: float) -> tuple[int, int]:
        """本拍两路输出 ``(刺激器, 惩罚器)``：

        * 刺激期 → 刺激强度（爬升中）；冷静期 → 冷静强度；释放期 →
          助力强度（立即满量不爬升）——刺激/助力共用刺激器这一个映射
          变量输出；待机 0。
        * 惩罚器：冷静期惩罚窗口内 → 惩罚器强度（映射行取最大值即惩罚
          优先），其余 0。
        """
        if self.phase == PHASE_STIM:
            return self._stim_target(now), 0
        if self.phase == PHASE_COOL:
            punish = max(0, self._i("punish_strength")) \
                if (self.punish_until > 0 and now < self.punish_until) else 0
            return max(0, self._i("cool_strength")), punish
        if self.phase == PHASE_RELEASE:
            return max(0, self._i("assist_strength")), 0
        return 0, 0

    def step(self, pressure: float | None, edge: int | None,
             fresh: bool, now: float) -> None:
        """推进一拍状态转移。

        :param pressure: 平滑 + 漏气补偿后气压 (kPa)；``fresh`` 为假时忽略
        :param edge: 官方边控状态 0-4（无会话为 None）
        :param fresh: 气压读数是否在 ``sensor_timeout_s`` 内（失联 fail-safe）
        :param now: monotonic 时间戳
        """
        dt = 0.0
        if self._last_now is not None:
            dt = min(MAX_STEP_DT, max(0.0, now - self._last_now))
        self._last_now = now
        mode = self._mode()
        if mode == "off" or not fresh:
            # off 模式不闭环；气压失联 fail-safe 归零待机
            self._enter(PHASE_IDLE, now)
            self._history.clear()
            return
        if pressure is not None:
            self._history.append((now, pressure))
        self._adapt_step(now, dt)
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
        次数/时长释放不介入，离开释放自动清零循环计数。"""
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
        """气压闭环（判定条件对标官方设置页）：

        * 刺激期：气压高于红线（可要求持续 N 秒）或短窗口上升速率达
          跳变阈值 → 判即将高潮，进冷静；会话时长达「持续时长释放」
          直接进释放；
        * 冷静期：满最小冷静时间且气压低于蓝线（可要求持续 N 秒）→
          恢复刺激；若边控次数或会话时长达释放条件 → 进释放。
        """
        if pressure is None:
            self._enter(PHASE_IDLE, now)
            return
        if self.phase == PHASE_IDLE:
            self._enter(PHASE_STIM, now)
            return
        if self.phase == PHASE_RELEASE:
            return                       # 由释放计时 / 失联 / 暂停退出
        red = self.red_threshold()
        blue = self.blue_threshold()
        if self.phase == PHASE_STIM:
            time_release = self._f("time_release_s")
            if (time_release > 0 and self.session_since is not None
                    and (now - self.session_since) >= time_release):
                self._enter(PHASE_RELEASE, now)
                return
            above = pressure >= red
            if above and self._above_since is None:
                self._above_since = now
            if not above:
                self._above_since = None
            hold = self._f("edge_hold_s")
            held = above and (hold <= 0
                              or (self._above_since is not None
                                  and now - self._above_since >= hold))
            jump = (self._f("jump_rise") > 0
                    and self._rise_rate(now) >= self._f("jump_rise"))
            if held or jump:
                self._enter(PHASE_COOL, now)
            return
        if self.phase == PHASE_COOL:
            below = pressure <= blue
            if below and self._below_since is None:
                self._below_since = now
            if not below:
                self._below_since = None
            cooldown = self._f("cooldown_s", 10.0)
            hold = self._f("recovery_hold_s")
            waited = (now - self.phase_since) >= cooldown
            held = below and (hold <= 0
                              or (self._below_since is not None
                                  and now - self._below_since >= hold))
            if waited and held:
                limit = self._i("cycle_limit")
                time_release = self._f("time_release_s")
                timed = (time_release > 0 and self.session_since is not None
                         and (now - self.session_since) >= time_release)
                if (0 < limit <= self.cycles) or timed:
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
            """映射派发的目标设备：指定家族的第一台，回退跳过 BMTR。"""
            state = self._b._safe_state()
            if state is None:
                return None
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

    # ---- 设备定位（映射表派发用） ----------------------------------------

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

    # ---- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._tick_loop())
        mode = str(self.config.get("mode") or "sensor")
        self.log(f"灵猫边控联动已启动（模式 {mode}，每 {TICK_S:g}s 一拍；"
                 f"红线 {self.config.get('edge_threshold')} kPa / "
                 f"蓝线 {self.config.get('recovery_threshold')} kPa）")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        # 经映射表归零：状态机归位待机并把两路输出清零重新求值——
        # 映射行结果变化（如刺激器 60→0）即派发归零；表无强度行则不动设备
        self.guard.reset(self._clock())
        self.engine.signal("stim_strength", 0)
        self.engine.signal("punish_strength", 0)
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
        """暂停/恢复闭环（负鼠按键动作）：暂停即状态机归位，下一拍两路
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
        # 漏气补偿：叠加到平滑读数上的固定偏移（腔体漏气读数偏低时调大）
        leak = self._cfg_f(cfg, "leak_comp", 0.0)
        pressure = (self._smoothed + leak) if self._smoothed is not None \
            else None

        if self.paused:
            self.guard.reset(now)
        else:
            self.guard.step(pressure if fresh else None, edge, fresh, now)
        self._log_offline(fresh, now)
        self._log_phase()

        stim, punish = self.guard.outputs(now)
        on_edge = 1 if (pressure is not None and fresh
                        and pressure >= self.guard.red_threshold()) else 0
        self.engine.signal("pressure", round(pressure or 0.0, 2))
        self.engine.signal("edge", int(edge) if edge is not None else 0)
        self.engine.signal("stim_strength", stim)
        self.engine.signal("punish_strength", punish)
        self.engine.signal("on_edge", on_edge)
        self.engine.signal("on_release",
                           1 if self.guard.phase == PHASE_RELEASE else 0)
        self.engine.signal("cycles", self.guard.cycles)
        self._refresh_last_values()

    def _refresh_last_values(self) -> None:
        self.last_values = {name: float(self.engine.signals.get(name, 0.0) or 0.0)
                            for name in PARAM_DEFS}

    def _log_phase(self) -> None:
        """阶段切换提示（每次变化一条，含当前循环计数与自适应阈值）。"""
        if self.guard.phase == self._last_phase:
            return
        prev, self._last_phase = self._last_phase, self.guard.phase
        if prev == PHASE_IDLE and self.guard.phase == PHASE_IDLE:
            return
        self.log(f"闭环阶段 → {PHASE_LABELS.get(self.guard.phase, self.guard.phase)}"
                 f"（循环 {self.guard.cycles}，红线 "
                 f"{self.guard.red_threshold():.1f} / 蓝线 "
                 f"{self.guard.blue_threshold():.1f} kPa）")

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
