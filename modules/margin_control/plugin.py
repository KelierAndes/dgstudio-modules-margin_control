"""灵猫边控联动模块：事件流驱动的灵猫气压 / 官方边控会话闭环。

模块读取灵猫（BMTR）气压（0-60 kPa）与官方边控会话状态（DG-Lab 4.0 App
经 Socket V4 上报的 ``edgeState`` 0-4），状态机只产出**事实**（相位、循环
计数、红线/蓝线、到边/恢复/释放/会话状态事件），全部**变量处理由事件流
驱动**：配置项 ``events`` 中的规则在事件发生时改写变量表，行为选择不再
使用设置项开关——

* 默认事件流（META["config"]["events"] 缺省值，宿主装载时写入
  config/margin_control.json）构造：官方会话接线（app_0…app_4 → 相位）、
  到边惩罚（定时回滚）、阈值自适应（边控后红线下降/超时缓降、蓝线跟随/
  回升）、相位 → 刺激器输出、边控 5 轮后请求释放；
* 删改规则即改变行为（如删除「边控 5 轮后释放」行即不限次数，加一行
  周期规则 ``{"name": "持续时长释放", "trigger": "period", "arg": 100,
  "where": {"session_time": {"min": 1800}, "phase": {"max": 2}},
  "actions": [{"var": "release_req", "value": 1}]}`` 即持续时长释放）；
* 配置项 ``temps`` 播种临时变量（自定义计数器/中间量），规则可引用；
* 设置项只保留取值类参数（判定阈值/时长、强度、自适应数值），事件流
  规则经同名镜像变量引用，随设置热更新。

**设备控制只经映射表**：状态机与事件流都不直接调用设备命令，全部输出由
输入映射表行引用 ``{stim_strength}`` 等变量落地（默认行取两路强度最大值
驱动 A/B 强度）。

另有负鼠按键动作「灵猫气压清零」「边控闭环暂停/恢复」。
"""

META = {
    "id": "margin_control",
    "name": "灵猫边控联动",
    "version": "0.6.0",
    "description": "灵猫气压 / 官方边控会话 → 事件流驱动闭环边控：状态机"
                   "产出事实，默认事件流构造惩罚/自适应/释放等行为选择，"
                   "设备控制只经映射表传递。",
    "settings_key": "margin_control",
    "default_enabled": False,
    "actions": ["margin_reset_pressure", "margin_guard_toggle"],
    "params": {
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
    },
    "config": {
        # ---- 基础设置 ----
        "sensor_slot": {
            "label": "灵猫设备 (slot_id)", "type": "str", "default": "",
            "group": "basic",
            "desc": "留空用第一台灵猫（多台时在「控制」页查看 slot_id）",
        },
        "smooth": {
            "label": "气压平滑", "type": "float",
            "default": 0.5, "min": 0.0, "max": 0.95, "step": 0.05,
            "group": "basic",
            "desc": "指数平滑系数，越大越稳（0 关闭平滑）",
        },
        "sensor_timeout_s": {
            "label": "气压失联判停 (秒)", "type": "float",
            "default": 5.0, "min": 1.0, "max": 60.0, "step": 0.5,
            "group": "basic",
            "desc": "超过该时长没有新的气压读数视为灵猫失联，闭环归零待机",
        },
        "leak_comp": {
            "label": "漏气补偿 (kPa)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 20.0, "step": 0.1,
            "group": "basic",
            "desc": "叠加到气压读数上的固定补偿（腔体漏气读数偏低时调大；"
                    "官方默认 3）",
        },
        # ---- 判定条件（取值类参数；行为选择由事件流构造） ----
        "edge_threshold": {
            "label": "边缘气压阈值 (kPa)", "type": "float",
            "default": 17.0, "min": 1.0, "max": 60.0, "step": 0.1,
            "group": "judge",
            "desc": "红线初始值：气压高于该值判断即将高潮（自适应累计在 "
                    "red 变量上；先用「气压清零」校准静息 0 点）",
        },
        "recovery_threshold": {
            "label": "恢复气压阈值 (kPa)", "type": "float",
            "default": 15.0, "min": 0.0, "max": 60.0, "step": 0.1,
            "group": "judge",
            "desc": "蓝线初始值：冷静期内气压低于该值判断恢复/冷静"
                    "（恒低于红线 0.5 kPa）",
        },
        "edge_hold_s": {
            "label": "持续高于红线判定 (秒)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 30.0, "step": 0.5,
            "group": "judge",
            "desc": "连续 N 秒高于红线才判断即将高潮，0=立即",
        },
        "jump_rise": {
            "label": "跳变阈值 (kPa/s)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 60.0, "step": 0.1,
            "group": "judge",
            "desc": "气压短时间快速上升达该速率也判断即将高潮"
                    "（官方「气压短时间上升差值」），0=关闭",
        },
        "jump_window_s": {
            "label": "跳变观测窗口 (秒)", "type": "float",
            "default": 2.0, "min": 0.5, "max": 10.0, "step": 0.5,
            "group": "judge",
            "desc": "计算气压上升速率的时间窗口",
        },
        "cooldown_s": {
            "label": "最小冷静时间 (秒)", "type": "float",
            "default": 10.0, "min": 0.0, "max": 300.0, "step": 0.5,
            "group": "judge",
            "desc": "到边后至少冷静该时长，再等气压回落才恢复刺激",
        },
        "recovery_hold_s": {
            "label": "持续低于蓝线判定 (秒)", "type": "float",
            "default": 5.0, "min": 0.0, "max": 30.0, "step": 0.5,
            "group": "judge",
            "desc": "连续 N 秒低于蓝线才判断彻底冷静、恢复刺激，0=立即",
        },
        "release_s": {
            "label": "释放时长 (秒)", "type": "float",
            "default": 15.0, "min": 0.0, "max": 3600.0, "step": 5.0,
            "group": "judge",
            "desc": "释放期持续该时长后循环计数清零、重新开始刺激；"
                    "0=保持释放直到暂停/失联",
        },
        # ---- 强度取值（事件流规则经设置镜像引用） ----
        "stim_strength": {
            "label": "刺激强度", "type": "int",
            "default": 60, "min": 0, "max": 200,
            "group": "strength",
            "desc": "刺激期目标强度（镜像变量 {stim_setting}，默认事件流"
                    "据此爬升输出）",
        },
        "cool_strength": {
            "label": "冷静期强度", "type": "int",
            "default": 0, "min": 0, "max": 200,
            "group": "strength",
            "desc": "冷静期维持强度（镜像变量 {cool_setting}，0=完全撤除）",
        },
        "assist_strength": {
            "label": "助力强度", "type": "int",
            "default": 80, "min": 0, "max": 200,
            "group": "strength",
            "desc": "释放期输出强度（镜像变量 {assist_setting}）",
        },
        "ramp_s": {
            "label": "刺激爬升时长 (秒)", "type": "float",
            "default": 3.0, "min": 0.0, "max": 60.0, "step": 0.5,
            "group": "strength",
            "desc": "进入刺激期从 0 缓升到目标强度（镜像变量 {ramp_s}），"
                    "0=立即",
        },
        # ---- 阈值自适应取值（规则引用；删除对应规则即关闭该自适应） ----
        "adapt_drop_pct": {
            "label": "每次边控后红线下降 (%)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 50.0, "step": 0.5,
            "group": "adapt",
            "desc": "默认事件流在到边事件中据此下调红线",
        },
        "adapt_drop_rate": {
            "label": "红线下降速度 (%/s)", "type": "float",
            "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.1,
            "group": "adapt",
            "desc": "默认事件流在刺激期超时后据此缓降红线",
        },
        "adapt_drop_delay_s": {
            "label": "无高潮红线缓降等待 (秒)", "type": "float",
            "default": 100.0, "min": 0.0, "max": 600.0, "step": 5.0,
            "group": "adapt",
            "desc": "刺激阶段该时长未到边，默认事件流开始缓降红线",
        },
        "adapt_blue_follow": {
            "label": "蓝线跟随下降 (%)", "type": "float",
            "default": 30.0, "min": 0.0, "max": 100.0, "step": 1.0,
            "group": "adapt",
            "desc": "默认事件流在到边事件中让蓝线跟随红线下降该百分比",
        },
        "adapt_blue_rise_rate": {
            "label": "蓝线上升速度 (%/s)", "type": "float",
            "default": 0.5, "min": 0.0, "max": 10.0, "step": 0.1,
            "group": "adapt",
            "desc": "默认事件流在冷静期超时后据此上调蓝线",
        },
        "adapt_blue_delay_s": {
            "label": "未恢复蓝线上升等待 (秒)", "type": "float",
            "default": 100.0, "min": 0.0, "max": 600.0, "step": 5.0,
            "group": "adapt",
            "desc": "冷静阶段该时长未恢复，默认事件流开始上调蓝线",
        },
        # ---- 事件流（行为选择在此构造，取代模式/惩罚/释放条件等开关） ----
        "events": {
            "label": "事件流", "type": "list",
            "default": [
                # 官方边控会话（Socket V4 edgeState 0-4）驱动相位
                {"name": "会话接线：刺激", "trigger": "event", "arg": "app_1",
                 "actions": [{"var": "phase", "value": 1}]},
                {"name": "会话接线：冷静", "trigger": "event", "arg": "app_2",
                 "actions": [{"var": "phase", "value": 2}]},
                {"name": "会话接线：冷静判定", "trigger": "event", "arg": "app_3",
                 "actions": [{"var": "phase", "value": 2}]},
                {"name": "会话接线：允许高潮", "trigger": "event", "arg": "app_4",
                 "actions": [{"var": "phase", "value": 3}]},
                {"name": "会话接线：停止", "trigger": "event", "arg": "app_0",
                 "actions": [{"var": "phase", "value": 0}]},
                # 到边 → 惩罚器输出（窗口后自动归零）
                {"name": "到边惩罚", "trigger": "event", "arg": "edge",
                 "actions": [{"var": "punish_strength", "value": 100,
                              "revert": 0, "after_s": 1.0}]},
                # 到边 → 红线按设置百分比下降，蓝线按比例跟随
                {"name": "边控后红线下降", "trigger": "event", "arg": "edge",
                 "actions": [{"var": "red",
                              "expr": "max(1, {red} - {red}"
                                      " * {adapt_drop_pct} / 100)"}]},
                {"name": "蓝线跟随下降", "trigger": "event", "arg": "edge",
                 "actions": [{"var": "blue",
                              "expr": "max(0, {blue} - ({edge_threshold}"
                                      " - {red}) * {adapt_blue_follow}"
                                      " / 100)"}]},
                # 刺激期（每 100ms）：刺激器按爬升时长趋向刺激强度
                {"name": "刺激期", "trigger": "period", "arg": 100,
                 "where": {"phase": {"min": 1, "max": 1}},
                 "actions": [{"var": "stim_strength",
                              "expr": "min({stim_setting},"
                                      " round({stim_setting}"
                                      " * {phase_time}"
                                      " / max(0.1, {ramp_s})))"}]},
                # 刺激期超时未到边 → 红线缓降（官方「N 秒内未判定高潮」）
                {"name": "刺激期红线缓降", "trigger": "period", "arg": 100,
                 "where": {"phase": {"min": 1, "max": 1},
                           "phase_time": {"min": "{adapt_drop_delay_s}"}},
                 "actions": [{"var": "red",
                              "expr": "max(1, {red} - {red}"
                                      " * {adapt_drop_rate} / 100 * 0.1)"}]},
                # 冷静期（每 100ms）：维持冷静期强度
                {"name": "冷静期", "trigger": "period", "arg": 100,
                 "where": {"phase": {"min": 2, "max": 2}},
                 "actions": [{"var": "stim_strength",
                              "expr": "{cool_setting}"}]},
                # 冷静期超时未恢复 → 蓝线缓升（官方「N 秒内未判定恢复刺激」）
                {"name": "冷静期蓝线回升", "trigger": "period", "arg": 100,
                 "where": {"phase": {"min": 2, "max": 2},
                           "phase_time": {"min": "{adapt_blue_delay_s}"}},
                 "actions": [{"var": "blue",
                              "expr": "min({red} - 0.5, {blue} + {blue}"
                                      " * {adapt_blue_rise_rate}"
                                      " / 100 * 0.1)"}]},
                # 释放期（每 100ms）：输出助力强度
                {"name": "释放期", "trigger": "period", "arg": 100,
                 "where": {"phase": {"min": 3}},
                 "actions": [{"var": "stim_strength",
                              "expr": "{assist_setting}"}]},
                # 待机期（每 100ms）：输出归零
                {"name": "待机期", "trigger": "period", "arg": 100,
                 "where": {"phase": {"max": 0}},
                 "actions": [{"var": "stim_strength", "value": 0}]},
                # 边控 5 轮后请求释放（官方「固定模式」；删掉本行即不限次数，
                # 持续时长释放示例：{"name": "持续时长释放", "trigger":
                # "period", "arg": 100, "where": {"session_time": {"min":
                # 1800}, "phase": {"max": 2}}, "actions": [{"var":
                # "release_req", "value": 1}]})
                {"name": "边控 5 轮后允许释放", "trigger": "event",
                 "arg": "recovered",
                 "where": {"cycles": {"min": 5}},
                 "actions": [{"var": "release_req", "value": 1}]},
            ],
            "group": "map",
            "desc": "事件流规则（按序执行）：行 {name: 规则名, trigger: "
                    "period|event, arg: 周期毫秒|事件名, where: 变量条件"
                    "{min/max}, actions: [{var, value|expr, revert?, "
                    "after_s?}]}。触发事件：tick / edge / recovered / "
                    "release / stim / app_0…app_4；表达式为四则运算 + "
                    "abs/min/max/round，引用 {} 变量。行为选择（官方会话"
                    "接线、惩罚、自适应、释放条件）都在此构造，删改行即"
                    "改变行为",
        },
        # ---- 临时变量（事件流可用的自定义中间量） ----
        "temps": {
            "label": "临时变量", "type": "list", "default": [],
            "group": "map",
            "desc": "行 {name: 变量名, value: 初值}，装载时播种进变量表"
                    "（重载不覆盖已有值），事件流与映射表表达式可引用"
                    "（自定义计数器、中间量等）",
        },
        # ---- 两张映射表之输入表（纯输入模块，无输出表） ----
        "mappings": {
            "label": "输入映射表", "type": "list", "default": [],
            "group": "map", "rows": "in",
            "desc": "行 {param: 核心输入参数, expr: 表达式}，表达式以 "
                    "{stim_strength} {punish_strength} {on_release} "
                    "{pressure} 等引用变量表（设备控制的唯一通道），结果"
                    "取整钳制后派发；表留空用默认行（刺激器/惩罚器最大值"
                    "驱动 A/B 强度）",
        },
    },
}

from plugins import ModuleBase, spec_defaults

from modules.margin_control import bridge as _bridge_mod
from modules.margin_control.bridge import (PARAM_DEFS, MarginBridge,
                                           MarginConfig)

# 配置缺省值唯一来源 = META["config"] 声明，MarginConfig 仅做兜底
MARGIN_CONFIG_DEFAULTS = spec_defaults(META["config"])


class MarginControlModule(ModuleBase):
    id = META["id"]
    name = META["name"]
    version = META["version"]
    description = META["description"]
    settings_key = META["settings_key"]

    def __init__(self):
        self.bridge: MarginBridge | None = None
        self.ctx = None

    def config_spec(self) -> dict:
        """配置项声明（宿主优先按实例声明渲染联动页设置区）。"""
        return dict(META["config"])

    def link_params(self) -> list[tuple[str, str]]:
        """映射变量表（事实 + 输出 + 设置镜像，与变量表一致）。"""
        return [(name, str(item.get("label") or ""))
                for name, item in PARAM_DEFS.items()]

    def on_load(self, ctx) -> None:
        self.ctx = ctx

    def on_unload(self) -> None:
        if self.bridge is not None:
            self.bridge.close()
        self.bridge = None
        self.ctx = None

    async def start(self) -> None:
        if self.bridge is not None and self.bridge._running:
            return
        if self.bridge is not None:
            try:
                await self.bridge.stop()
            except Exception:
                pass
        self.bridge = MarginBridge(
            MarginConfig(self.ctx.settings, defaults=MARGIN_CONFIG_DEFAULTS),
            self.ctx.engine.get_state,
            self.ctx.engine,
            events=self.ctx.events,
        )
        self.bridge.log = self.ctx.log
        await self.bridge.start()

    async def reload_config(self) -> None:
        """映射表、事件流与设置镜像编辑后立即热生效。"""
        if self.bridge is None:
            return
        for key in MARGIN_CONFIG_DEFAULTS:
            if key in self.ctx.settings:
                self.bridge.config[key] = self.ctx.settings[key]
        await self.bridge.reload_config()

    async def stop(self) -> None:
        if self.bridge is not None:
            await self.bridge.stop()

    def is_running(self) -> bool:
        return self.bridge is not None and bool(getattr(self.bridge,
                                                        "_running", False))

    # ---- 负鼠按键动作（§3：回调在引擎线程，快速返回） -------------------

    def button_actions(self) -> list:
        from plugins import ButtonAction

        return [
            ButtonAction("margin_reset_pressure", "灵猫气压清零",
                         on_press=self._press_reset_pressure),
            ButtonAction("margin_guard_toggle", "边控闭环 暂停/恢复",
                         on_press=self._press_guard_toggle),
        ]

    def _press_reset_pressure(self, slot_id, argument) -> None:
        """灵猫气压清零（校准静息 0 点；仅蓝牙直连灵猫支持）。"""
        if self.ctx is None:
            return
        fut = self.ctx.submit(self.ctx.engine.reset_pressure())
        fut.add_done_callback(self._log_future)

    def _press_guard_toggle(self, slot_id, argument) -> None:
        """暂停/恢复闭环（暂停即相位归位，输出经事件流+映射表归零）。"""
        if self.ctx is None:
            return
        if self.bridge is None:
            self.ctx.log("边控闭环尚未启动（先在模块页启动模块）")
            return
        self.bridge.toggle_pause()

    def _log_future(self, fut) -> None:
        exc = fut.exception()
        if exc is not None and self.ctx is not None:
            self.ctx.log(f"气压清零失败: {exc!r}")
