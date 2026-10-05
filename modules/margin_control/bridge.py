"""灵猫边控桥接：事件流驱动的变量处理 + 核心映射（唯一设备通道）。

架构（v0.5.0 起）：状态机只产出**事实**（相位、循环计数、红线/蓝线、
到边/恢复/释放事件、官方边控会话状态变化），全部**变量处理由事件流驱动**
——配置文件中的 ``events`` 规则列表在事件发生时改写变量表，输出变量
（刺激器/惩罚器强度）与行为选择（惩罚、阈值自适应、释放条件、官方会话
接线）全部由默认事件流构造，不再使用设置项开关。

事件流规则（配置项 ``events``，按声明顺序执行）::

    {"on": "edge",                          # 触发事件（见下）
     "where": {"cycles": {"min": 5}},       # 可选：变量条件（min/max，含边界，
                                            #   值可为数字或表达式字符串）
     "set": {"release_req": 1},             # 变量赋值（数字或表达式字符串，
                                            #   按声明顺序依次生效）
     "revert": {"punish_strength": 0},      # 可选：after_s 秒后回滚的赋值
     "after_s": 1.0}

触发事件：``tick``（每拍）、``edge``（判到边）、``recovered``（恢复刺激）、
``release``（进入释放期）、``stim``（进入/回到刺激期）、``app_0``…
``app_4``（官方边控会话状态变化）。表达式经核心安全求值（四则运算 +
abs/min/max/round，不支持比较运算），未定义变量按 0 处理。

变量表（事实 + 输出 + 设置镜像）全部经映射引擎发布，设备控制仍只经
输入映射表（默认行 ``max({stim_strength}, {punish_strength})`` 驱动 A/B
强度）。

默认事件流（META["config"]["events"] 缺省，宿主装载时写入配置文件）构造：

* 官方边控会话接线（app_0…app_4 → 相位，取代旧「边控模式」选择）；
* 到边惩罚（1 秒后自动归零，取代旧「惩罚器强度/时长」设置）；
* 阈值自适应（边控后红线按 % 下降、蓝线跟随；超时未到边红线缓降、
  超时未恢复蓝线缓升——取代旧「自适应开关」设置）；
* 相位 → 刺激器输出（刺激期爬升 / 冷静期维持 / 释放期助力 / 待机归零）；
* 边控 5 轮后请求释放（取代旧「循环上限」设置；持续时长释放等其它条件
  按同 schema 自行加行，如 ``{"on": "tick", "where": {"session_time":
  {"min": 1800}, "phase": {"max": 2}}, "set": {"release_req": 1}}``）。

安全设计不变：气压失联 fail-safe 归零待机、暂停即相位归零（输出经映射
表归零）、停止经映射表归零、蓝线恒低于红线 0.5 kPa。
"""

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

__all__ = ["MarginBridge", "MarginConfig", "EdgeGuard", "EventStream",
           "PARAM_DEFS", "MIRROR_KEYS", "DEFAULT_MAPPINGS",
           "PHASE_IDLE", "PHASE_STIM", "PHASE_COOL", "PHASE_RELEASE",
           "PHASE_LABELS"]

# 闭环节拍：0.1s 一拍（与核心脉冲帧 / 音频联动的节奏一致）
TICK_S = 0.1
# 单拍最大按秒推进量：暂停/失联恢复后自适应不瞬移
MAX_STEP_DT = 5.0
# 红蓝线安全间隙 (kPa)：蓝线恒不超过红线 - GAP
THRESHOLD_GAP = 0.5
# 事件流可用的触发事件
EVENTS = ("tick", "edge", "recovered", "release", "stim",
          "app_0", "app_1", "app_2", "app_3", "app_4")

# 闭环相位（事实变量 ``phase``）
PHASE_IDLE = 0
PHASE_STIM = 1
PHASE_COOL = 2
PHASE_RELEASE = 3
PHASE_LABELS = {PHASE_IDLE: "待机", PHASE_STIM: "刺激",
                PHASE_COOL: "冷静", PHASE_RELEASE: "释放"}

# 设置镜像：配置值以同名变量进入变量表，事件流规则可直接引用并随设置热更新
MIRROR_KEYS: dict[str, str] = {
    "stim_setting": "stim_strength",
    "cool_setting": "cool_strength",
    "assist_setting": "assist_strength",
    "ramp_s": "ramp_s",
    "edge_threshold": "edge_threshold",
    "recovery_threshold": "recovery_threshold",
    "adapt_drop_pct": "adapt_drop_pct",
    "adapt_drop_rate": "adapt_drop_rate",
    "adapt_drop_delay_s": "adapt_drop_delay_s",
    "adapt_blue_follow": "adapt_blue_follow",
    "adapt_blue_rise_rate": "adapt_blue_rise_rate",
    "adapt_blue_delay_s": "adapt_blue_delay_s",
}

# 变量表（事实 + 输出 + 设置镜像）：全部经映射引擎发布，映射表/事件流
# 表达式均可引用；META["params"] 的唯一来源
PARAM_DEFS: dict[str, dict[str, str]] = {
    # ---- 事实变量（状态机产出） ----
    "pressure": {"label": "灵猫气压", "desc": "平滑 + 漏气补偿后气压 (kPa)"},
    "edge": {"label": "官方边控状态", "desc": "App 边控会话 0-4：0 停止 / "
                                            "1 刺激 / 2 冷静计时 / 3 冷静判定 / 4 允许高潮"},
    "phase": {"label": "闭环相位", "desc": "0 待机 / 1 刺激 / 2 冷静 / 3 释放"
                                          "（事件流可直接改写）"},
    "phase_time": {"label": "相位时长", "desc": "当前相位已持续秒数"},
    "session_time": {"label": "会话时长", "desc": "本次运行首次进入刺激期以来"
                                                "的秒数（持续时长释放条件用）"},
    "red": {"label": "红线", "desc": "当前边缘阈值 (kPa)，自适应累计后值，"
                                    "事件流可改写"},
    "blue": {"label": "蓝线", "desc": "当前恢复阈值 (kPa)，恒低于红线 0.5"},
    "cycles": {"label": "边控循环", "desc": "当前轮「到边→冷静」计数"},
    "on_edge": {"label": "到边标志", "desc": "气压 ≥ 当前红线时为 1"},
    "on_release": {"label": "释放标志", "desc": "释放期（允许高潮）为 1"},
    # ---- 输出变量（默认事件流构造） ----
    "stim_strength": {"label": "刺激器强度", "desc": "由事件流按相位构造：刺激"
                                                   "期爬升刺激强度、冷静期维持冷静强度、"
                                                   "释放期输出助力强度（0-200）"},
    "punish_strength": {"label": "惩罚器强度", "desc": "由事件流在到边事件构造，"
                                                     "惩罚窗口后自动归零（0-200）"},
    # ---- 设置镜像（规则可引用，随设置热更新） ----
    "stim_setting": {"label": "设置镜像：刺激强度", "desc": "设置「刺激强度」"
                                                        "的当前值"},
    "cool_setting": {"label": "设置镜像：冷静期强度", "desc": "设置「冷静期强度」"
                                                        "的当前值"},
    "assist_setting": {"label": "设置镜像：助力强度", "desc": "设置「助力强度」"
                                                        "的当前值"},
    "ramp_s": {"label": "设置镜像：刺激爬升时长", "desc": "设置「刺激爬升时长"
                                                        "(秒)」的当前值"},
    "edge_threshold": {"label": "设置镜像：边缘气压阈值", "desc": "红线初始值"
                                                              "(kPa)，自适应累计在 red 变量上"},
    "recovery_threshold": {"label": "设置镜像：恢复气压阈值", "desc": "蓝线初始值"
                                                                "(kPa)"},
    "adapt_drop_pct": {"label": "设置镜像：边控后红线下降", "desc": "设置「每次"
                                                              "边控后红线下降(%)」的当前值"},
    "adapt_drop_rate": {"label": "设置镜像：红线下降速度", "desc": "设置「红线"
                                                              "下降速度(%/s)」的当前值"},
    "adapt_drop_delay_s": {"label": "设置镜像：红线缓降等待", "desc": "设置「无"
                                                              "高潮红线缓降等待(秒)」的当前值"},
    "adapt_blue_follow": {"label": "设置镜像：蓝线跟随下降", "desc": "设置「蓝线"
                                                              "跟随下降(%)」的当前值"},
    "adapt_blue_rise_rate": {"label": "设置镜像：蓝线上升速度", "desc": "设置「蓝"
                                                              "线上升速度(%/s)」的当前值"},
    "adapt_blue_delay_s": {"label": "设置镜像：蓝线上升等待", "desc": "设置「未"
                                                              "恢复蓝线上升等待(秒)」的当前值"},
}

# 映射表为空时的默认行：刺激器/惩罚器取最大值驱动郊狼/负鼠 A/B 强度；
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
        "sensor_slot": "",
        "smooth": 0.5,
        "sensor_timeout_s": 5.0,
        "leak_comp": 0.0,
        # 判定条件（阈值/时长类取值，行为选择由事件流构造）
        "edge_threshold": 17.0,
        "recovery_threshold": 15.0,
        "edge_hold_s": 0.0,
        "jump_rise": 0.0,
        "jump_window_s": 2.0,
        "cooldown_s": 10.0,
        "recovery_hold_s": 5.0,
        "release_s": 15.0,
        # 强度取值（事件流规则经设置镜像引用）
        "stim_strength": 60,
        "cool_strength": 0,
        "assist_strength": 80,
        "ramp_s": 3.0,
        # 自适应取值（规则引用；删除对应规则即关闭该自适应）
        "adapt_drop_pct": 0.0,
        "adapt_drop_rate": 1.0,
        "adapt_drop_delay_s": 100.0,
        "adapt_blue_follow": 30.0,
        "adapt_blue_rise_rate": 0.5,
        "adapt_blue_delay_s": 100.0,
        # 事件流与映射表
        "events": [],
        "temps": [],
        "mappings": [],
    }

    def __init__(self, data: dict | None = None, defaults: dict | None = None):
        super().__init__({k: v for k, v in
                          (defaults or self.DEFAULTS).items()})
        if data:
            self.update({k: v for k, v in data.items() if v is not None})


# ---------------------------------------------------------------- 事件流

class EventStream:
    """事件流规则引擎：触发器 → 条件 → 动作（变量赋值，含定时回滚）。

    规则按声明顺序执行，同一拍内后面的动作能读到前面刚写入的值。
    表达式经核心 :mod:`dglab.expr` 安全求值（四则运算 + abs/min/max/round，
    不支持比较运算），求值失败跳过该条赋值并限流记日志。

    规则 schema::

        {"name": "刺激期",                # 可选：规则名（日志/可读性）
         "trigger": "period",            # period=周期触发 | event=事件触发
         "arg": 100,                     # period: 周期(毫秒)；event: 事件名
         "where": {"phase": {"min": 1}}, # 可选：变量条件（min/max 含边界，
                                         #   值为数字或表达式字符串）
         "actions": [                    # 动作列表（变量赋值）
           {"var": "stim_strength", "expr": "…"},             # 表达式赋值
           {"var": "punish_strength", "value": 100,           # 直接赋值
            "revert": 0, "after_s": 1.0}                     # 可选：定时回滚
         ]}

    触发事件（event 的 arg）：``tick``（每拍）、``edge``（判到边）、
    ``recovered``（恢复刺激）、``release``（进入释放期）、``stim``（进入/
    回到刺激期）、``app_0``…``app_4``（官方边控会话状态变化）。

    兼容 v0.5 行 schema：``{"on": 事件名, "where": …, "set": {变量: 值},
    "revert": {…}, "after_s": …}``（等价于 event 触发 + 集中赋值动作）。
    """

    def __init__(self, log: Callable[[str], None] | None = None):
        self.log = log or (lambda msg: None)
        self.event_rules: dict[str, list[dict]] = {}
        self.period_rules: list[dict] = []
        self._reverts: list[tuple[float, dict]] = []   # (到期时刻, 赋值表)
        self._acc: list[float] = []                    # 周期规则累计器
        self._last_tick: float | None = None
        self._last_error_log = float("-inf")

    def load(self, rows: Any) -> None:
        """装载规则列表（非法行跳过并记日志）。"""
        self.event_rules = {}
        self.period_rules = []
        self._reverts.clear()
        self._acc = []
        self._last_tick = None
        for i, row in enumerate(rows or []):
            if not isinstance(row, dict):
                self.log(f"事件流第 {i + 1} 行不是对象，已跳过")
                continue
            name = str(row.get("name") or f"规则{i + 1}")
            where = row.get("where") or {}
            if "trigger" in row or "arg" in row:
                self._load_typed(name, where, row, i)
            else:
                self._load_legacy(name, where, row, i)

    def _load_typed(self, name: str, where: dict, row: dict, i: int) -> None:
        """新 schema：trigger = period | event + actions 动作列表。"""
        trigger = str(row.get("trigger") or "")
        arg = row.get("arg")
        actions = self._parse_actions(row.get("actions"), i, name)
        if trigger == "period":
            period_ms = self._num(arg, 100.0)
            if period_ms is None or not actions:
                self.log(f"事件流「{name}」缺少 arg(毫秒)/actions，已跳过")
                return
            self.period_rules.append({
                "name": name, "period": max(0.0, period_ms) / 1000.0,
                "where": where, "actions": actions})
            self._acc.append(0.0)
        elif trigger == "event":
            on = str(arg or "")
            if not on or not actions:
                self.log(f"事件流「{name}」缺少 arg(事件名)/actions，已跳过")
                return
            self.event_rules.setdefault(on, []).append(
                {"name": name, "where": where, "actions": actions})
        else:
            self.log(f"事件流「{name}」触发器 {trigger!r} 未知，已跳过")

    def _load_legacy(self, name: str, where: dict, row: dict, i: int) -> None:
        """兼容 v0.5 行 schema：on / set / revert / after_s。"""
        on = str(row.get("on") or "")
        set_ops = row.get("set")
        if not on or not isinstance(set_ops, dict) or not set_ops:
            self.log(f"事件流第 {i + 1} 行（{name}）缺少 trigger/arg/actions"
                     "或 on/set，已跳过")
            return
        actions = [{"var": var, "value": raw} for var, raw in set_ops.items()]
        self.event_rules.setdefault(on, []).append({
            "name": name, "where": where, "actions": actions,
            "revert": row.get("revert") or {},
            "after_s": self._num(row.get("after_s"), 0.0)})

    def _parse_actions(self, rows: Any, i: int, name: str) -> list[dict]:
        """动作列表解析：{var, value|expr, revert?, after_s?}。"""
        out: list[dict] = []
        for action in rows or []:
            if not isinstance(action, dict):
                self.log(f"事件流「{name}」有动作不是对象，已跳过")
                continue
            var = str(action.get("var") or "")
            if not var:
                self.log(f"事件流「{name}」有动作缺少 var，已跳过")
                continue
            entry: dict = {"var": var}
            if "expr" in action:
                entry["value"] = str(action["expr"])
            elif "value" in action:
                entry["value"] = action["value"]
            else:
                self.log(f"事件流「{name}」动作 {var!r} 缺少 value/expr，"
                         "已跳过")
                continue
            entry["revert"] = action.get("revert")
            entry["after_s"] = self._num(action.get("after_s"), 0.0)
            out.append(entry)
        return out

    def reset(self) -> None:
        """清空待回滚队列与周期累计（停止/重载时）。"""
        self._reverts.clear()
        self._acc = [0.0] * len(self.period_rules)
        self._last_tick = None

    def dispatch(self, events: list[str], vars: dict, now: float) -> None:
        """派发：事件规则按本拍事件触发；周期规则按 arg 毫秒节奏触发。"""
        dt = 0.0
        if self._last_tick is not None:
            dt = min(1.0, max(0.0, now - self._last_tick))
        self._last_tick = now
        for event in events:
            for rule in self.event_rules.get(event, ()):
                if self._match(rule["where"], vars):
                    self._apply(rule, vars, now)
        for i, rule in enumerate(self.period_rules):
            acc = self._acc[i] + dt
            if rule["period"] <= 0 or acc >= rule["period"] - 1e-9:
                acc = 0.0
                if self._match(rule["where"], vars):
                    self._apply(rule, vars, now)
            self._acc[i] = acc
        self._fire_due(vars, now)

    # ---- 内部 -----------------------------------------------------------

    @staticmethod
    def _num(raw: Any, default: float | None = None) -> float | None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _value(self, raw: Any, vars: dict) -> float | None:
        """赋值/条件取值：数字直读，字符串按表达式求值（失败返回 None）。"""
        if not isinstance(raw, str):
            return self._num(raw)
        try:
            return float(_expr.evaluate(raw, vars))
        except _expr.ExprError as exc:
            now = time.monotonic()
            if now - self._last_error_log > 30.0:
                self._last_error_log = now
                self.log(f"事件流表达式求值失败（30 秒内不再重复提示）: "
                         f"{raw!r} → {exc}")
            return None

    def _match(self, where: dict, vars: dict) -> bool:
        for var, bounds in (where or {}).items():
            value = self._num(vars.get(var, 0.0), 0.0)
            if not isinstance(bounds, dict):
                bounds = {"min": bounds}
            for kind, raw in bounds.items():
                bound = self._value(raw, vars)
                if bound is None:
                    return False
                if kind == "min" and value < bound - 1e-9:
                    return False
                if kind == "max" and value > bound + 1e-9:
                    return False
        return True

    def _apply(self, rule: dict, vars: dict, now: float) -> None:
        for action in rule["actions"]:
            value = self._value(action["value"], vars)
            if value is not None:
                vars[action["var"]] = value
            revert = action.get("revert")
            after = float(action.get("after_s") or 0.0)
            if revert is not None and after > 0:
                self._reverts.append((now + after, {action["var"]: revert}))
        # 兼容 v0.5 行级 revert（set 赋值后的集中回滚）
        if rule.get("revert") and (rule.get("after_s") or 0.0) > 0:
            self._reverts.append((now + rule["after_s"],
                                  dict(rule["revert"])))

    def _fire_due(self, vars: dict, now: float) -> None:
        if not self._reverts:
            return
        due = [item for item in self._reverts if item[0] <= now]
        if not due:
            return
        self._reverts = [item for item in self._reverts if item[0] > now]
        for _due_at, revert in due:
            for var, raw in revert.items():
                value = self._value(raw, vars)
                if value is not None:
                    vars[var] = value


# ---------------------------------------------------------------- 状态机

class EdgeGuard:
    """边控状态机（事实产出）：只负责判定与相位推进，不做变量输出。

    相位、循环计数、红线/蓝线、释放请求都存放在共享变量表 ``vars`` 中
    （事件流规则可改写；机器每拍读取并推进）。每拍返回发生的事件名列表
    （``edge`` / ``recovered`` / ``release`` / ``stim`` / ``app_N``），
    由桥接器连同 ``tick`` 交给事件流。

    判定条件（取值类设置）：红线/蓝线（变量，自适应在变量上累计）、
    持续高于/低于判定、气压跳变速率、最小冷静时间、释放时长。
    """

    def __init__(self, config: dict, vars: dict):
        self.config = config
        self.vars = vars
        self.phase_since = 0.0
        self.session_since: float | None = None   # 会话开始（首次进刺激）
        self._phase = 0                            # 上次同步的相位
        self._last_app: int | None = None
        self._seen_pressure = False
        self._above_since: float | None = None
        self._below_since: float | None = None
        self._history: list[tuple[float, float]] = []

    # ---- 配置快捷读取 ---------------------------------------------------

    def _f(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.config.get(key))
        except (TypeError, ValueError):
            return default

    # ---- 生命周期 -------------------------------------------------------

    def reset(self, now: float = 0.0) -> None:
        """归位待机（暂停/停止用）：相位归零，保留会话与自适应累计。"""
        self.vars["phase"] = float(PHASE_IDLE)
        self.phase_since = now
        self._above_since = None
        self._below_since = None
        self._history.clear()

    def step(self, pressure: float | None, official: int | None,
             fresh: bool, now: float) -> list[str]:
        """推进一拍，返回本拍发生的事件名列表。"""
        events: list[str] = []
        v = self.vars

        # 官方边控会话状态变化 → app_N 事件
        if official is not None:
            state = max(0, min(4, int(official)))
            if state != self._last_app:
                self._last_app = state
                events.append(f"app_{state}")

        if not fresh:
            # 气压失联 fail-safe：曾有读数则归零待机（从未有过则不强制，
            # 纯官方会话场景仍可由 app 事件驱动）
            if self._seen_pressure:
                v["phase"] = float(PHASE_IDLE)
            self._history.clear()
            self._sync_phase(now)
            self._update_times(now)
            return events

        if pressure is not None:
            self._seen_pressure = True
            self._history.append((now, pressure))

        phase = int(v.get("phase", 0) or 0)

        # 释放请求（事件流经 release_req 命令）：刺激/冷静期 → 释放
        if float(v.get("release_req", 0) or 0) > 0:
            v["release_req"] = 0.0
            if phase in (PHASE_STIM, PHASE_COOL):
                phase = PHASE_RELEASE
                events.append("release")
        elif phase == PHASE_RELEASE:
            release_s = self._f("release_s")
            if release_s > 0 and (now - self.phase_since) >= release_s:
                phase = PHASE_STIM
                v["cycles"] = 0.0
                events.append("stim")

        if phase == PHASE_IDLE and pressure is not None:
            phase = PHASE_STIM
            events.append("stim")
        elif phase == PHASE_STIM and pressure is not None:
            if self._detect_edge(pressure, now):
                phase = PHASE_COOL
                v["cycles"] = float(v.get("cycles", 0) or 0) + 1.0
                events.append("edge")
        elif phase == PHASE_COOL and pressure is not None:
            if self._detect_recovery(pressure, now):
                phase = PHASE_STIM
                events.append("recovered")

        v["phase"] = float(phase)
        self._sync_phase(now)
        self._update_times(now)
        return events

    # ---- 判定 ----

    def _detect_edge(self, pressure: float, now: float) -> bool:
        """到边判定：气压 ≥ 红线（可要求持续 N 秒）或短窗口上升速率达
        跳变阈值。"""
        red = float(self.vars.get("red", self._f("edge_threshold", 17.0)))
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
        return held or jump

    def _detect_recovery(self, pressure: float, now: float) -> bool:
        """恢复判定：满最小冷静时间且气压 ≤ 蓝线（可要求持续 N 秒）。"""
        blue = float(self.vars.get("blue", self._f("recovery_threshold", 15.0)))
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
        return waited and held

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

    # ---- 相位/时间同步 ----

    def _sync_phase(self, now: float) -> None:
        phase = int(self.vars.get("phase", 0) or 0)
        if phase != self._phase:
            self._phase = phase
            self.phase_since = now
            self._above_since = None
            self._below_since = None
            if phase == PHASE_STIM and self.session_since is None:
                self.session_since = now

    def _update_times(self, now: float) -> None:
        self.vars["phase_time"] = max(0.0, now - self.phase_since)
        if self.session_since is not None:
            self.vars["session_time"] = max(0.0, now - self.session_since)


# ---------------------------------------------------------------- 桥接器

class MarginBridge:
    """灵猫边控运行时：传感读取 + 事实状态机 + 事件流 + 映射表派发。

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
        self._smoothed: float | None = None
        self._last_pressure_at: float | None = None
        self._last_offline_log = float("-inf")
        self._last_phase: int = PHASE_IDLE
        self.last_values: dict[str, float] = {}

        # 共享变量表：事实 + 输出 + 设置镜像（事件流与状态机读写）
        self.vars: dict[str, float] = {}
        self._seed_vars()
        self._guard = EdgeGuard(self.config, self.vars)

        # 事件流（规则来自配置 events）与映射引擎（设备控制唯一通道）
        self.stream = EventStream(self.log)
        self.engine = MappingEngine(self._dispatch,
                                    device_vars=self._device_vars,
                                    ranges=input_ranges())
        self._api = self._DeviceApi(self)
        self.dispatchers = build_dispatchers(self._api, core_inputs())
        self._primed = False
        self.apply_config()

    # ---- 变量表 ---------------------------------------------------------

    def _seed_vars(self) -> None:
        """变量表播种：事实归零，红线/蓝线取判定阈值，镜像取设置值。"""
        cfg = self.config
        seed: dict[str, float] = {
            "phase": float(PHASE_IDLE), "cycles": 0.0, "release_req": 0.0,
            "phase_time": 0.0, "session_time": 0.0,
            "on_edge": 0.0, "on_release": 0.0,
            "stim_strength": 0.0, "punish_strength": 0.0,
            "pressure": 0.0, "edge": 0.0,
        }
        for var, key in MIRROR_KEYS.items():
            seed[var] = self._cfg_f(cfg, key, 0.0)
        seed["red"] = seed["edge_threshold"]
        seed["blue"] = seed["recovery_threshold"]
        self.vars.update(seed)

    def _refresh_mirrors(self) -> None:
        """设置热更新：仅刷新镜像变量（红线/蓝线/相位等累计状态保留）。"""
        for var, key in MIRROR_KEYS.items():
            if key in self.config:
                try:
                    self.vars[var] = float(self.config[key])
                except (TypeError, ValueError):
                    pass

    def _clamp_invariants(self) -> None:
        """变量不变式：红线 ≥ 1，蓝线 ∈ [0, 红线 − 0.5]。"""
        v = self.vars
        v["red"] = max(1.0, float(v.get("red", 17.0) or 0.0))
        v["blue"] = min(max(0.0, float(v.get("blue", 15.0) or 0.0)),
                        v["red"] - THRESHOLD_GAP)

    # ---- 映射表与事件流装载 ---------------------------------------------

    def apply_config(self) -> None:
        """装载映射表与事件流；首轮映射只静默求值（不把设备写成 0）。"""
        first = not self._primed
        if first:
            self.engine.armed = False
        self.engine.set_mappings(self._effective_rows())
        self._seed_temps()
        self.stream.load(self.config.get("events") or [])
        if first:
            self.engine.armed = True
            self._primed = True

    def _seed_temps(self) -> None:
        """播种配置的临时变量（temps：行 {name, value}）；重载不覆盖已有值。"""
        for temp in self.config.get("temps") or []:
            if not isinstance(temp, dict):
                continue
            name = str(temp.get("name") or "").strip()
            if not name:
                continue
            try:
                value = float(temp.get("value") or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            self.vars.setdefault(name, value)

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
        rule_count = len(self.stream.period_rules) + sum(
            len(rules) for rules in self.stream.event_rules.values())
        self.log(f"灵猫边控联动已启动（事件流 {rule_count} 条规则，"
                 f"红线 {self.vars.get('red')} kPa / "
                 f"蓝线 {self.vars.get('blue')} kPa）")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        # 经映射表归零：输出变量清零重新求值；表无强度行则不动设备
        self.guard_reset()
        self.stream.reset()
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

    def guard_reset(self) -> None:
        """状态机归位待机（相位归零，保留会话与自适应累计）。"""
        self._guard.reset(self._clock())

    async def reload_config(self) -> None:
        """联动页保存设置后由宿主调用：映射表、事件流与镜像热生效。"""
        self._refresh_mirrors()
        self.apply_config()

    def toggle_pause(self) -> bool:
        """暂停/恢复闭环（负鼠按键动作）：暂停即相位归位，下一拍输出变量
        经事件流/映射表归零，恢复后重新爬升。"""
        self.paused = not self.paused
        self.log("边控闭环已暂停（输出经映射表归零）" if self.paused
                 else "边控闭环已恢复")
        return self.paused

    # ---- 节拍 -----------------------------------------------------------

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
        official = slot.edge_state if slot is not None else None
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

        # 状态机推进（事实 + 事件）
        if self.paused:
            self.guard_reset()
            fired: list[str] = []
        else:
            fired = self._guard.step(pressure if fresh else None, official,
                                     fresh, now)
        self._log_offline(fresh, now)

        # 事件流：状态机事件 + 每拍 tick → 变量赋值（含定时回滚）
        self.stream.dispatch(fired + ["tick"], self.vars, now)
        self._clamp_invariants()
        self._log_phase()

        # 事实标志与传感变量
        pressure_eff = pressure if (pressure is not None and fresh) else None
        self.vars["on_edge"] = 1.0 if (
                pressure_eff is not None
                and pressure_eff >= float(self.vars.get("red", 17.0))) else 0.0
        self.vars["on_release"] = 1.0 if (
                int(self.vars.get("phase", 0) or 0) == PHASE_RELEASE) else 0.0
        self.vars["pressure"] = round(pressure or 0.0, 2)
        self.vars["edge"] = float(official) if official is not None else 0.0

        # 变量表 → 映射引擎（变化才泵） → 输入映射表派发设备动作
        for name, value in list(self.vars.items()):
            self.engine.signal(name, value)
        self._refresh_last_values()

    def _refresh_last_values(self) -> None:
        self.last_values = {name: float(self.vars.get(name, 0.0) or 0.0)
                            for name in PARAM_DEFS}

    def _log_phase(self) -> None:
        """相位切换提示（每次变化一条，含当前循环计数与红线/蓝线）。"""
        phase = int(self.vars.get("phase", 0) or 0)
        if phase == self._last_phase:
            return
        prev, self._last_phase = self._last_phase, phase
        if prev == PHASE_IDLE and phase == PHASE_IDLE:
            return
        self.log(f"闭环相位 → {PHASE_LABELS.get(phase, phase)}"
                 f"（循环 {int(self.vars.get('cycles', 0) or 0)}，红线 "
                 f"{float(self.vars.get('red', 0.0)):.1f} / 蓝线 "
                 f"{float(self.vars.get('blue', 0.0)):.1f} kPa）")

    @staticmethod
    def _cfg_f(cfg: dict, key: str, default: float) -> float:
        try:
            return float(cfg.get(key))
        except (TypeError, ValueError):
            return default

    def _log_offline(self, fresh: bool, now: float) -> None:
        """气压失联提示：每 30s 至多一条（无灵猫时提示绑定缺失）。"""
        if self.paused or fresh:
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
