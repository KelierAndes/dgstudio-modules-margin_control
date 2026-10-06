"""灵猫边控联动模块：把气压传感器与官方边控会话接入输出设备闭环。

模块读取灵猫（BMTR）气压（0-60 kPa）与官方边控会话状态（DG-Lab 4.0 App
经 Socket V4 上报的 ``edgeState`` 0-4，核心控制页「边控状态」同款语义），
按配置模式跑**模块自动维护**的闭环状态机（判定条件与阈值自适应对标
DG-Lab 官方边控玩法设置页，状态机内部直接计算并维护全部输出变量，不经
事件流），产出两路强度映射变量（刺激器 / 惩罚器）：

* ``sensor`` 气压闭环：红线/蓝线 + 持续判定 + 气压跳变判到边；最小冷静
  时间满且气压回落恢复；边控次数或会话时长达限进释放期（助力强度经刺激
  器变量输出）；阈值自适应（边控后红线下降、超时缓降、蓝线跟随/回升）；
* ``app`` 跟随官方边控会话：刺激/冷静/允许高潮四态驱动变量；
* ``off`` 只提供映射变量。

**设备控制只经映射表**：状态机不直接调用设备命令，全部输出由输入映射表
行引用 ``{stim_strength}`` 等变量落地（默认行取两路强度最大值驱动 A/B
强度，可改写为任意核心输入参数）。

META["config"] 声明全部配置项（分组对齐官方设置页），宿主装载
config/margin_control.json 时自动补齐缺省，联动页据此渲染映射表与模块
设置；另有负鼠按键动作「灵猫气压清零」「边控闭环暂停/恢复」。
"""

META = {
    "id": "margin_control",
    "name": "灵猫边控联动",
    "version": "0.8.0",
    "description": "灵猫气压 / 官方边控会话 → 闭环边控（模块自动维护）："
                   "红线/蓝线 + 持续判定 + 气压跳变 + 有界阈值自适应，"
                   "边控次数或时长达限释放；派发按家族严格定位防跨设备"
                   "串扰，设备控制只经映射表传递。",
    "settings_key": "margin_control",
    "default_enabled": False,
    "actions": ["margin_reset_pressure", "margin_guard_toggle"],
    "params": {
        "pressure": {"label": "灵猫气压", "desc": "平滑 + 漏气补偿后气压 (kPa)"},
        "edge": {"label": "官方边控状态", "desc": "App 边控会话 0-4：0 停止 / "
                                                "1 刺激 / 2 冷静计时 / 3 冷静判定 / 4 允许高潮"},
        "stim_strength": {"label": "刺激器强度", "desc": "刺激期按爬升时长趋向"
                                                       "刺激强度，冷静期维持冷静强度，释放期输出"
                                                       "助力强度，其余 0（0-200）"},
        "punish_strength": {"label": "惩罚器强度", "desc": "到边进冷静后的惩罚"
                                                         "输出，惩罚时长内非零（0-200）"},
        "on_edge": {"label": "到边标志", "desc": "气压 ≥ 当前边缘阈值（自适应后"
                                                "红线）时为 1"},
        "on_release": {"label": "释放标志", "desc": "释放期（允许高潮，助力输出）"
                                                  "为 1，其余 0"},
        "cycles": {"label": "边控循环", "desc": "当前轮「到边→冷静」计数；释放完成"
                                              "后清零重新计"},
    },
    "config": {
        # ---- 基础设置 ----
        "mode": {
            "label": "边控模式", "type": "choice",
            "choices": ["sensor", "app", "off"], "default": "sensor",
            "group": "basic",
            "desc": "sensor=按气压判定自动边控（不依赖 App）；"
                    "app=跟随官方 App 边控会话（Socket V4，状态 0-4）；"
                    "off=不闭环，仅提供映射变量",
        },
        "sensor_slot": {
            "label": "灵猫设备 (slot_id)", "type": "str", "default": "",
            "group": "basic",
            "desc": "留空用第一台灵猫（多台时在「控制」页查看 slot_id）",
        },
        "output_slot": {
            "label": "目标输出设备 (slot_id)", "type": "str", "default": "",
            "group": "basic",
            "desc": "映射表强度派发的落点：绑定后全部强度行都驱动该设备"
                    "（不再按家族改判）；留空按映射行家族严格解析——家族"
                    "设备离线时不跨设备回退（防止负鼠/郊狼互相串扰）",
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
        # ---- 判定条件（对标官方「判定条件」页） ----
        "edge_threshold": {
            "label": "边缘气压阈值 (kPa)", "type": "float",
            "default": 17.0, "min": 1.0, "max": 60.0, "step": 0.1,
            "group": "judge",
            "desc": "红线：气压高于该值判断即将高潮（参与阈值自适应；"
                    "先用「气压清零」校准静息 0 点）",
        },
        "recovery_threshold": {
            "label": "恢复气压阈值 (kPa)", "type": "float",
            "default": 15.0, "min": 0.0, "max": 60.0, "step": 0.1,
            "group": "judge",
            "desc": "蓝线：冷静期内气压低于该值判断恢复/冷静"
                    "（与红线保持安全间隙，构成回差防抖）",
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
        # ---- 释放（对标官方「允许高潮释放」条件） ----
        "cycle_limit": {
            "label": "边控次数释放 (轮)", "type": "int",
            "default": 5, "min": 0, "max": 99,
            "group": "release",
            "desc": "指定边控次数后允许高潮释放（官方「固定模式」），"
                    "0=不限制（仅 sensor 模式）",
        },
        "time_release_s": {
            "label": "持续时长释放 (秒)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 14400.0, "step": 30.0,
            "group": "release",
            "desc": "游戏进行指定时长后允许高潮释放（官方同款条件），"
                    "0=关闭（仅 sensor 模式）",
        },
        "release_s": {
            "label": "释放时长 (秒)", "type": "float",
            "default": 15.0, "min": 0.0, "max": 3600.0, "step": 5.0,
            "group": "release",
            "desc": "释放期持续该时长后循环计数清零、重新开始刺激；"
                    "0=保持释放直到暂停/失联（仅 sensor 模式计时）",
        },
        # ---- 强度设置（对标官方「刺激器设置」页） ----
        "stim_strength": {
            "label": "刺激强度", "type": "int",
            "default": 60, "min": 0, "max": 200,
            "group": "strength",
            "desc": "刺激期目标强度，经映射变量 {stim_strength} 落地"
                    "（波形/上限请在官方 App 或控制页调好）",
        },
        "cool_strength": {
            "label": "冷静期强度", "type": "int",
            "default": 0, "min": 0, "max": 200,
            "group": "strength",
            "desc": "冷静期维持强度（0=完全撤除刺激）",
        },
        "assist_strength": {
            "label": "助力强度", "type": "int",
            "default": 80, "min": 0, "max": 200,
            "group": "strength",
            "desc": "释放期（允许高潮）输出强度；与刺激强度共用映射变量 "
                    "{stim_strength} 输出",
        },
        "ramp_s": {
            "label": "刺激爬升时长 (秒)", "type": "float",
            "default": 3.0, "min": 0.0, "max": 60.0, "step": 0.5,
            "group": "strength",
            "desc": "进入刺激期从 0 缓升到目标强度，0=立即",
        },
        "punish_strength": {
            "label": "惩罚器强度", "type": "int",
            "default": 100, "min": 0, "max": 200,
            "group": "strength",
            "desc": "判定到边的瞬间以该强度输出（经映射变量 "
                    "{punish_strength}），0=关闭惩罚",
        },
        "punish_s": {
            "label": "惩罚时长 (秒)", "type": "float",
            "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.5,
            "group": "strength",
            "desc": "惩罚输出的持续时长，0=关闭惩罚",
        },
        # ---- 阈值自适应调整（对标官方「阈值自适应调整」） ----
        "adapt_stim": {
            "label": "刺激阶段红线自适应", "type": "bool",
            "default": True,
            "group": "adapt",
            "desc": "刺激阶段按下列规则下调红线（更容易判到边）",
        },
        "adapt_drop_pct": {
            "label": "每次边控后红线下降 (%)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 50.0, "step": 0.5,
            "group": "adapt",
            "desc": "每次成功边控后红线阈值立即下降当前值的百分比",
        },
        "adapt_drop_delay_s": {
            "label": "无高潮红线缓降等待 (秒)", "type": "float",
            "default": 100.0, "min": 0.0, "max": 600.0, "step": 5.0,
            "group": "adapt",
            "desc": "刺激阶段该时长内未判定到边，红线开始缓慢下降",
        },
        "adapt_drop_rate": {
            "label": "红线下降速度 (%/s)", "type": "float",
            "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.1,
            "group": "adapt",
            "desc": "红线缓慢下降的速率（当前红线值的百分比/秒）",
        },
        "adapt_blue_follow": {
            "label": "蓝线跟随下降 (%)", "type": "float",
            "default": 30.0, "min": 0.0, "max": 100.0, "step": 1.0,
            "group": "adapt",
            "desc": "红线每下降 1 kPa，蓝线跟随下降该百分比",
        },
        "adapt_cool": {
            "label": "冷静阶段蓝线自适应", "type": "bool",
            "default": True,
            "group": "adapt",
            "desc": "冷静阶段超时未恢复时上调蓝线（恢复变容易）",
        },
        "adapt_blue_delay_s": {
            "label": "未恢复蓝线上升等待 (秒)", "type": "float",
            "default": 100.0, "min": 0.0, "max": 600.0, "step": 5.0,
            "group": "adapt",
            "desc": "冷静阶段该时长内未判定恢复刺激，蓝线开始上升",
        },
        "adapt_blue_rise_rate": {
            "label": "蓝线上升速度 (%/s)", "type": "float",
            "default": 0.5, "min": 0.0, "max": 10.0, "step": 0.1,
            "group": "adapt",
            "desc": "蓝线上升的速率（当前蓝线值的百分比/秒），"
                    "恒不越过红线",
        },
        "leak_comp": {
            "label": "漏气补偿 (kPa)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 20.0, "step": 0.1,
            "group": "adapt",
            "desc": "叠加到气压读数上的固定补偿（腔体漏气读数偏低时调大；"
                    "官方默认 3）",
        },
        # ---- 两张映射表之输入表（纯输入模块，无输出表） ----
        "mappings": {
            "label": "输入映射表", "type": "list", "default": [],
            "group": "map", "rows": "in",
            "desc": "行 {param: 核心输入参数, expr: 表达式}，表达式以 "
                    "{stim_strength} {punish_strength} {pressure} "
                    "{on_release} 等引用边控变量（设备控制的唯一通道），"
                    "结果取整钳制后派发；表留空用默认行（刺激器/惩罚器"
                    "最大值驱动 A/B 强度）",
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
        """映射变量表（模块可写参数：气压 / 边控状态 / 两路强度 / 标志）。"""
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
        """映射表与闭环参数编辑后立即热生效。"""
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
        """暂停/恢复闭环（暂停即两路输出归零，经映射表落地）。"""
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
