
from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, Callable

from dglab import expr as _expr
from dglab.mapping import MappingEngine
from dglab.params import (build_dispatchers, core_alias_values, core_inputs,
                          device_state_values, input_ranges)
from dglab.state import family_of

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
        "adapt_stim": True,
        "adapt_drop_pct": 0.0,
        "adapt_drop_delay_s": 100.0,
        "adapt_drop_rate": 1.0,
        "adapt_blue_follow": 30.0,
        "adapt_cool": True,
        "adapt_blue_rise_rate": 0.5,
        "adapt_blue_delay_s": 100.0,
        "leak_comp": 0.0,
        "mappings": [],
        "outputs": [],
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
        if not self._b("adapt_stim", True):
            return
        pct = self._f("adapt_drop_pct")
        if pct > 0:
            self._drop_red(self.red_threshold() * pct / 100.0)

    def _adapt_step(self, now: float, dt: float) -> None:
        if dt <= 0:
            return
        if self.phase == PHASE_STIM and self._b("adapt_stim", True):
            delay = self._f("adapt_drop_delay_s")
            rate = self._f("adapt_drop_rate")
            if rate > 0 and (now - self.phase_since) >= delay:
                self._drop_red(self._f("edge_threshold", 17.0)
                               * rate / 100.0 * dt)
        elif self.phase == PHASE_COOL and self._b("adapt_cool", True):
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


class MarginBridge:

    def __init__(self, config: MarginConfig, get_state: Callable[[], Any],
                 commands: Any, events=None):
        self.config = config
        self.get_state = get_state
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

        self.engine = MappingEngine(self._dispatch,
                                    device_vars=self._device_vars,
                                    ranges=input_ranges())
        self._api = self._DeviceApi(self)
        self.dispatchers = build_dispatchers(self._api, core_inputs())
        self._primed = False
        self.apply_config()


    def apply_config(self) -> None:
        first = not self._primed
        if first:
            self.engine.armed = False
        self.engine.set_mappings(self._effective_rows())
        self.engine.set_outputs(self._effective_output_rows())
        self._seed_temps()
        if first:
            self.engine.armed = True
            self._primed = True

    def _seed_temps(self) -> None:
        for temp in self.config.get("temps") or []:
            if not isinstance(temp, dict):
                continue
            name = str(temp.get("name") or "").strip()
            if not name or "expr" in temp:
                continue
            try:
                value = float(temp.get("value") or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            self.engine.signals.setdefault(name, value)

    def _rows_of(self, key: str) -> list[dict[str, str]]:
        return [row for row in (self.config.get(key) or [])
                if isinstance(row, dict)
                and str(row.get("param") or "").strip()]

    def _rows_from_events(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for event in self.config.get("events") or []:
            if not isinstance(event, dict):
                continue
            for action in event.get("actions") or []:
                if not isinstance(action, dict):
                    continue
                if str(action.get("dir") or "in") != "in":
                    continue
                param = str(action.get("param") or "").strip()
                var = str(action.get("var") or "").strip()
                if param and var:
                    out.append({"param": param, "expr": "{" + var + "}"})
        return out

    def _effective_rows(self) -> list[dict]:
        user = self._rows_of("mappings")
        if user:
            return user
        return self._rows_from_events()

    def _effective_output_rows(self) -> list[dict]:
        return self._rows_of("outputs")

    def _publish_temps(self) -> None:
        for temp in self.config.get("temps") or []:
            if not isinstance(temp, dict):
                continue
            name = str(temp.get("name") or "").strip()
            expr = temp.get("expr")
            if not name or not isinstance(expr, str) or not expr.strip():
                continue
            try:
                value = float(_expr.evaluate(expr, self.engine.signals))
            except _expr.ExprError:
                continue
            self.engine.signal(name, value)

    def _safe_state(self):
        try:
            return self.get_state()
        except Exception:
            return None

    def _device_vars(self) -> dict[str, float]:
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

        def __init__(self, bridge: "MarginBridge"):
            self._b = bridge

        @property
        def _cmd(self):
            return self._b.commands

        def resolve_slot(self, family: str = "") -> str | None:
            state = self._b._safe_state()
            if state is None:
                return None
            slots = state.slots or {}
            if family:
                for sid in sorted(slots):
                    if family_of(slots[sid].type) == family:
                        return sid
                return None
            for sid in sorted(slots):
                if family_of(slots[sid].type) != "BMTR":
                    return sid
            return None

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
        self.log("边控闭环已暂停（输出经映射表归零）" if self.paused
                 else "边控闭环已恢复")
        return self.paused


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
        self.engine.pump()
        raw = self.engine.out_values.get("pressure")
        edge = self.engine.out_values.get("edge")
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
        self._publish_temps()
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
                     "或检查输出映射表的 BMTR.Pressure 行）；闭环待机中")
        else:
            self.log("灵猫气压无有效读数（归零/失联/读数冻结），"
                     "闭环归零待机中")

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
