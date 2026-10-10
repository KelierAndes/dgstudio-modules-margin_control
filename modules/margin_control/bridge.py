
from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, Callable

from dglab.mapping import as_number
from dglab.params import core_alias_values, device_state_values

__all__ = ["MarginBridge", "MarginConfig", "EdgeGuard", "PARAM_DEFS",
           "PHASE_IDLE", "PHASE_STIM", "PHASE_COOL", "PHASE_RELEASE",
           "PHASE_LABELS"]

TICK_S = 0.1
FRESH_EPSILON = 0.05
MAX_STEP_DT = 5.0
THRESHOLD_GAP = 0.5

PHASE_IDLE = 0
PHASE_STIM = 1
PHASE_COOL = 2
PHASE_RELEASE = 3
PHASE_LABELS = {PHASE_IDLE: "待机", PHASE_STIM: "刺激",
                PHASE_COOL: "冷静", PHASE_RELEASE: "释放"}

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
    "cycles": {"label": "边控循环", "desc": "当前轮「到边→冷静」计数；释放边"
                                          "置零重新计"},
}


class MarginConfig(dict):

    DEFAULTS = {
        "mode": "sensor",
        "smooth": 0.5,
        "sensor_timeout_s": 5.0,
        "edge_threshold": 17.0,
        "recovery_threshold": 15.0,
        "edge_hold_s": 0.0,
        "jump_rise": 0.0,
        "jump_window_s": 2.0,
        "cooldown_s": 10.0,
        "recovery_hold_s": 5.0,
        "cycle_limit": 5,
        "time_release_s": 0.0,
        "release_s": 15.0,
        "stim_strength": 60,
        "cool_strength": 0,
        "assist_strength": 80,
        "ramp_s": 3.0,
        "punish_strength": 100,
        "punish_s": 1.0,
        "adapt_stim": False,
        "adapt_drop_pct": 0.0,
        "adapt_drop_delay_s": 100.0,
        "adapt_drop_rate": 1.0,
        "adapt_blue_follow": 30.0,
        "adapt_cool": False,
        "adapt_blue_rise_rate": 0.5,
        "adapt_blue_delay_s": 100.0,
        "leak_comp": 0.0,
    }

    def __init__(self, data: dict | None = None, defaults: dict | None = None):
        super().__init__({k: v for k, v in
                          (defaults or self.DEFAULTS).items()})
        if data:
            self.update({k: v for k, v in data.items() if v is not None})


class EdgeGuard:

    def __init__(self, config: dict):
        self.config = config
        self.phase = PHASE_IDLE
        self.phase_since = 0.0
        self.cycles = 0
        self.punish_until = 0.0
        self.session_since: float | None = None
        self.red_drop = 0.0
        self.blue_shift = 0.0
        self._above_since: float | None = None
        self._below_since: float | None = None
        self._history: list[tuple[float, float]] = []
        self._last_now: float | None = None


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


    def red_floor(self) -> float:
        return max(1.0, self._f("recovery_threshold", 15.0) + THRESHOLD_GAP)

    def red_threshold(self) -> float:
        return max(self.red_floor(),
                   self._f("edge_threshold", 17.0) - self.red_drop)

    def blue_threshold(self) -> float:
        blue = self._f("recovery_threshold", 15.0) + self.blue_shift
        return min(max(0.0, blue), self.red_threshold() - THRESHOLD_GAP)

    def _drop_red(self, amount: float) -> None:
        if amount <= 0:
            return
        room = max(0.0, self.red_threshold() - self.red_floor())
        amount = min(amount, room)
        if amount <= 0:
            return
        self.red_drop += amount
        follow = self._f("adapt_blue_follow")
        if follow > 0:
            self.blue_shift -= amount * follow / 100.0

    def _adapt_on_edge(self, now: float) -> None:
        if not self._b("adapt_stim", False):
            return
        pct = self._f("adapt_drop_pct")
        if pct > 0:
            self._drop_red(self.red_threshold() * pct / 100.0)

    def _adapt_step(self, now: float, dt: float) -> None:
        if dt <= 0:
            return
        if self.phase == PHASE_STIM and self._b("adapt_stim", False):
            delay = self._f("adapt_drop_delay_s")
            rate = self._f("adapt_drop_rate")
            if rate > 0 and (now - self.phase_since) >= delay:
                self._drop_red(self._f("edge_threshold", 17.0)
                               * rate / 100.0 * dt)
        elif self.phase == PHASE_COOL and self._b("adapt_cool", False):
            delay = self._f("adapt_blue_delay_s")
            rate = self._f("adapt_blue_rise_rate")
            if rate > 0 and (now - self.phase_since) >= delay:
                self.blue_shift += self.blue_threshold() * rate / 100.0 * dt

    def _rise_rate(self, now: float) -> float:
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


    def reset(self, now: float = 0.0) -> None:
        self.phase = PHASE_IDLE
        self.phase_since = now
        self.punish_until = 0.0
        self._above_since = None
        self._below_since = None
        self._history.clear()

    def _enter(self, phase: int, now: float) -> None:
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
        stim = max(0, self._i("stim_strength", 60))
        ramp = self._f("ramp_s")
        if ramp <= 0:
            return stim
        t = min(1.0, max(0.0, (now - self.phase_since) / ramp))
        return int(round(stim * t))

    def outputs(self, now: float) -> tuple[int, int]:
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
        dt = 0.0
        if self._last_now is not None:
            dt = min(MAX_STEP_DT, max(0.0, now - self._last_now))
        self._last_now = now
        mode = self._mode()
        if mode == "off" or not fresh:
            self._enter(PHASE_IDLE, now)
            self._history.clear()
            return
        if pressure is not None:
            self._history.append((now, pressure))
        self._adapt_step(now, dt)
        if mode == "app":
            self._step_app(edge, now)
            return
        if self.phase == PHASE_RELEASE:
            release_s = self._f("release_s")
            if release_s > 0 and (now - self.phase_since) >= release_s:
                self._enter(PHASE_STIM, now)
        self._step_sensor(pressure, now)

    def _step_app(self, edge: int | None, now: float) -> None:
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
        if pressure is None:
            self._enter(PHASE_IDLE, now)
            return
        if self.phase == PHASE_IDLE:
            self._enter(PHASE_STIM, now)
            return
        if self.phase == PHASE_RELEASE:
            return
        red = self.red_threshold()
        blue = self.blue_threshold()
        if self.phase == PHASE_STIM:
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
                if self._release_allowed(now):
                    self._enter(PHASE_RELEASE, now)
                    self.cycles = 0
                else:
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
                self._enter(PHASE_STIM, now)

    def _release_allowed(self, now: float) -> bool:
        limit = self._i("cycle_limit")
        if 0 < limit <= self.cycles:
            return True
        time_release = self._f("time_release_s")
        return (time_release > 0 and self.session_since is not None
                and (now - self.session_since) >= time_release)


class _SignalBoard:
    """闭环的变量面板：模块只登记读数，不派发设备。

    宿主按 ``bridge.engine`` 取实时值（``plugins._mapping_engine`` 的兼容挂表、
    ``flow_host.module_signals`` 的事件流读数、``ui/live.py`` 的概览），需要的是
    ``signals`` / ``errors`` / ``pump()`` 三件。``temps`` 由宿主
    ``apply_logic_tables()`` 换成**全局共享变量表**：事件流的「写入变量」卡片把
    ``BMTR.Pressure`` / ``BMTR.EdgeState`` 写成 ``pressure`` / ``edge``，
    闭环每拍就从这里取输入。

    原来的派发层（``MappingEngine`` + ``build_dispatchers`` + ``_DeviceApi`` +
    模块内事件卡片）随「模块不许直写设备」的新规整体移除：刺激 / 惩罚强度改由
    事件流的写入卡片读 ``{stim_strength}`` / ``{punish_strength}`` 两个变量落地。
    ``mappings`` / ``outputs`` 等映射表时代的属性只作为恒空兼容位保留，
    ``ui/live.py`` 的通道概览仍会直接读它们。
    """

    def __init__(self, device_vars: Callable[[], dict[str, float]] | None = None):
        self._device_vars = device_vars or (lambda: {})
        self.signals: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.temps: dict[str, float] = {}
        self.last_values: dict[str, float] = {}
        self.mappings: dict[str, str] = {}
        self.outputs: list[dict[str, Any]] = []
        self.out_values: dict[str, Any] = {}
        self.out_errors: dict[str, str] = {}

    def signal(self, name: str, value: Any) -> None:
        num = as_number(value)
        if num is None:
            return
        self.signals[str(name)] = num

    def values(self) -> dict[str, float]:
        """取值口径：设备读数 < 共享变量 < 本模块发布的读数（与旧映射引擎一致）。"""
        merged = self._device_vars()
        for key, value in self.temps.items():
            merged.setdefault(str(key), value)
        merged.update(self.signals)
        return merged

    def attach_temps(self, shared: dict[str, float]) -> None:
        if shared is not self.temps:
            shared.update(self.temps)
            self.temps = shared

    def pump(self) -> None:
        return None

    def reset(self) -> None:
        self.signals.clear()
        self.errors.clear()
        self.last_values.clear()


class MarginBridge:

    def __init__(self, config: MarginConfig, get_state: Callable[[], Any],
                 commands: Any = None, events=None):
        self.config = config
        self.get_state = get_state
        # 设备命令入口只为老调用点保留，闭环不再碰它：模块直写设备已被宿主拦下，
        # 强度落地改由事件流的写入卡片读 {stim_strength} / {punish_strength}。
        self.commands = commands

        self.log: Callable[[str], None] = print
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._clock: Callable[[], float] = time.monotonic

        self.paused = False
        self.guard = EdgeGuard(config)
        self._smoothed: float | None = None
        self._last_pressure_seen: float | None = None
        self._last_pressure_change: float | None = None
        self._last_offline_log = float("-inf")
        self._last_phase: int = PHASE_IDLE
        self.last_values: dict[str, float] = {}

        self.engine = _SignalBoard(self._device_vars)

    def apply_config(self) -> None:
        """派发层已移除：闭环每拍直读 ``self.config``，这里没有要装载的表。"""
        return None

    def _safe_state(self):
        try:
            return self.get_state()
        except Exception:
            return None

    def _device_vars(self) -> dict[str, float]:
        vals = device_state_values(self._safe_state())
        vals.update(core_alias_values(vals))
        return vals

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
        self.guard.reset(self._clock())
        self.engine.signal("stim_strength", 0)
        self.engine.signal("punish_strength", 0)
        self._refresh_last_values()
        self.log("灵猫边控联动已停止")

    def close(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def reload_config(self) -> None:
        self.apply_config()

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        self.log("边控闭环已暂停（输出经事件流归零）" if self.paused
                 else "边控闭环已恢复")
        return self.paused


    async def _tick_loop(self) -> None:
        last_error_log = float("-inf")
        try:
            while self._running:
                await asyncio.sleep(TICK_S)
                try:
                    self._tick()
                except Exception:
                    # 持续异常时限流：日志页每条都全量重建，0.1s 一栈会卡死
                    now = time.monotonic()
                    if now - last_error_log >= 30.0:
                        last_error_log = now
                        self._log_error("边控节拍失败")
        except asyncio.CancelledError:
            pass

    def _tick(self) -> None:
        cfg = self.config
        self.engine.pump()
        raw = self.engine.temps.get("pressure")
        edge = self.engine.temps.get("edge")
        now = self._clock()

        timeout = max(1.0, self._cfg_f(cfg, "sensor_timeout_s", 5.0))
        if raw is not None and raw != self._last_pressure_seen:
            self._last_pressure_seen = raw
            self._last_pressure_change = now
        changed_recently = (self._last_pressure_change is not None
                            and now - self._last_pressure_change <= timeout)

        smooth = min(0.95, max(0.0, self._cfg_f(cfg, "smooth", 0.5)))
        if raw is not None:
            if self._smoothed is None or smooth <= 0:
                self._smoothed = float(raw)
            else:
                self._smoothed += (1.0 - smooth) * (float(raw) - self._smoothed)
        leak = self._cfg_f(cfg, "leak_comp", 0.0)
        pressure = (self._smoothed + leak) if self._smoothed is not None \
            else None
        fresh = (pressure is not None and pressure > FRESH_EPSILON
                 and changed_recently)

        if self.paused:
            self.guard.reset(now)
        else:
            self.guard.step(pressure if fresh else None, edge, fresh, now)
        self._log_offline(fresh, now)
        self._log_phase()

        stim, punish = self.guard.outputs(now)
        on_edge = 1 if fresh and pressure >= self.guard.red_threshold() else 0
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
        mode = str(self.config.get("mode") or "sensor")
        if self.paused or mode == "off" or fresh:
            return
        if now - self._last_offline_log < 30.0:
            return
        self._last_offline_log = now
        if self._last_pressure_seen is None:
            self.log("尚未读到有效气压（>0 kPa；确认灵猫已连接，"
                     "事件流已把 BMTR.Pressure 写入 pressure 变量）；闭环待机中")
        else:
            self.log("灵猫气压无有效读数（归零/失联/读数冻结），"
                     "闭环归零待机中")

    def _log_error(self, prefix: str) -> None:
        self.log(f"{prefix}:\n{traceback.format_exc()}")
