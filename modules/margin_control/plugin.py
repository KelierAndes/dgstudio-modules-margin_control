"""灵猫边控联动模块：把气压传感器与官方边控会话接入输出设备闭环。

模块读取灵猫（BMTR）气压（0-60 kPa）与官方边控会话状态（DG-Lab 4.0 App
经 Socket V4 上报的 ``edgeState`` 0-4，核心控制页「边控状态」同款语义），
按配置模式跑闭环状态机，产出两路强度映射变量（刺激器 / 惩罚器）：

* ``sensor`` 气压闭环：到边阈值撤除刺激 → 冷静期满且气压回落恢复；循环达
  「循环上限」后进入释放期（刺激器持续输出），释放期满清零计数重新开始；
* ``app`` 跟随官方边控会话：刺激/冷静/允许高潮四态驱动变量；
* ``off`` 只提供映射变量。

**设备控制只经映射表**：状态机不直接调用设备命令，全部输出由输入映射表
行引用 ``{stim_strength}`` 等变量落地（默认行取两路强度最大值驱动 A/B
强度，可改写为任意核心输入参数）。

META["config"] 声明全部配置项，宿主装载 config/margin_control.json 时自动
补齐缺省，联动页据此渲染映射表与模块设置；另有负鼠按键动作「灵猫气压
清零」「边控闭环暂停/恢复」。
"""

META = {
    "id": "margin_control",
    "name": "灵猫边控联动",
    "version": "0.3.0",
    "description": "灵猫气压 / 官方边控会话 → 闭环边控：按气压阈值自动"
                   "「刺激→到边→冷静→恢复」，循环达上限后释放（刺激器持续"
                   "输出），或跟随 App 边控状态；设备控制只经映射表传递。",
    "settings_key": "margin_control",
    "default_enabled": False,
    "actions": ["margin_reset_pressure", "margin_guard_toggle"],
    "params": {
        "pressure": {"label": "灵猫气压", "desc": "平滑后气压 (kPa)"},
        "edge": {"label": "官方边控状态", "desc": "App 边控会话 0-4：0 停止 / "
                                                "1 刺激 / 2 冷静计时 / 3 冷静判定 / 4 允许高潮"},
        "stim_strength": {"label": "刺激器强度", "desc": "刺激期按爬升时长趋向"
                                                       "刺激器强度，冷静期维持冷静强度，释放期持续输出"
                                                       "刺激器强度，其余 0（0-200）"},
        "punish_strength": {"label": "惩罚器强度", "desc": "到边进冷静后的惩罚"
                                                         "输出，惩罚时长内非零（0-200）"},
        "on_edge": {"label": "到边标志", "desc": "平滑气压 ≥ 边缘阈值时为 1"},
        "cycles": {"label": "边控循环", "desc": "当前轮「到边→冷静」计数；释放完成"
                                              "后清零重新计"},
    },
    "config": {
        # ---- 边控玩法 ----
        "mode": {
            "label": "边控模式", "type": "choice",
            "choices": ["sensor", "app", "off"], "default": "sensor",
            "group": "edge",
            "desc": "sensor=按气压阈值自动边控（不依赖 App）；"
                    "app=跟随官方 App 边控会话（Socket V4，状态 0-4）；"
                    "off=不闭环，仅提供映射变量",
        },
        "edge_threshold": {
            "label": "边缘阈值 (kPa)", "type": "float",
            "default": 40.0, "min": 5.0, "max": 60.0, "step": 0.5,
            "group": "edge",
            "desc": "气压升到该值判定到达临界，撤除刺激进入冷静"
                    "（先用「气压清零」校准静息 0 点）",
        },
        "recovery_threshold": {
            "label": "恢复阈值 (kPa)", "type": "float",
            "default": 20.0, "min": 0.0, "max": 60.0, "step": 0.5,
            "group": "edge",
            "desc": "冷静期内气压回落到该值以下才允许恢复刺激"
                    "（与边缘阈值构成回差防抖）",
        },
        "cooldown_s": {
            "label": "冷静最短时长 (秒)", "type": "float",
            "default": 10.0, "min": 0.0, "max": 300.0, "step": 0.5,
            "group": "edge",
            "desc": "到边后至少冷静该时长，再等气压回落才恢复刺激",
        },
        "stim_strength": {
            "label": "刺激器强度", "type": "int",
            "default": 60, "min": 0, "max": 200,
            "group": "edge",
            "desc": "刺激期目标强度，释放期（循环上限达成或 App「允许高潮」）"
                    "持续输出的强度；经映射变量 {stim_strength} 落地"
                    "（波形/上限请在官方 App 或控制页调好）",
        },
        "cool_strength": {
            "label": "冷静期强度", "type": "int",
            "default": 0, "min": 0, "max": 200,
            "group": "edge",
            "desc": "冷静期维持强度（0=完全撤除刺激）",
        },
        "ramp_s": {
            "label": "刺激爬升时长 (秒)", "type": "float",
            "default": 3.0, "min": 0.0, "max": 60.0, "step": 0.5,
            "group": "edge",
            "desc": "进入刺激期从 0 缓升到目标强度，0=立即",
        },
        "punish_strength": {
            "label": "惩罚器强度", "type": "int",
            "default": 100, "min": 0, "max": 200,
            "group": "edge",
            "desc": "判定到边的瞬间以该强度输出（经映射变量 "
                    "{punish_strength}），0=关闭惩罚",
        },
        "punish_s": {
            "label": "惩罚时长 (秒)", "type": "float",
            "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.5,
            "group": "edge",
            "desc": "惩罚输出的持续时长，0=关闭惩罚",
        },
        "release_s": {
            "label": "释放时长 (秒)", "type": "float",
            "default": 15.0, "min": 0.0, "max": 300.0, "step": 0.5,
            "group": "edge",
            "desc": "释放期持续该时长后循环计数清零、重新开始刺激；"
                    "0=保持释放直到暂停/失联（仅 sensor 模式计时）",
        },
        "cycle_limit": {
            "label": "循环上限 (轮)", "type": "int",
            "default": 0, "min": 0, "max": 99,
            "group": "edge",
            "desc": "边控循环达到该轮数后允许释放：冷静期满改为进入释放期，"
                    "刺激器以刺激器强度持续输出；0=不限制（仅 sensor 模式）",
        },
        "smooth": {
            "label": "气压平滑", "type": "float",
            "default": 0.5, "min": 0.0, "max": 0.95, "step": 0.05,
            "group": "edge",
            "desc": "指数平滑系数，越大越稳（0 关闭平滑）",
        },
        "sensor_timeout_s": {
            "label": "气压失联判停 (秒)", "type": "float",
            "default": 5.0, "min": 1.0, "max": 60.0, "step": 0.5,
            "group": "edge",
            "desc": "超过该时长没有新的气压读数视为灵猫失联，闭环归零待机",
        },
        # ---- 设备绑定 ----
        "sensor_slot": {
            "label": "灵猫设备 (slot_id)", "type": "str", "default": "",
            "group": "device",
            "desc": "留空用第一台灵猫（多台时在「控制」页查看 slot_id）",
        },
        # ---- 两张映射表之输入表（纯输入模块，无输出表） ----
        "mappings": {
            "label": "输入映射表", "type": "list", "default": [],
            "group": "map", "rows": "in",
            "desc": "行 {param: 核心输入参数, expr: 表达式}，表达式以 "
                    "{stim_strength} {punish_strength} {pressure} "
                    "等引用边控变量（设备控制的唯一通道），结果取整钳制后"
                    "派发；表留空用默认行（刺激器/惩罚器最大值驱动 A/B 强度）",
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
        """映射变量表（模块可写参数：气压 / 边控状态 / 三路强度 / 循环等）。"""
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
        """暂停/恢复闭环（暂停即三路输出归零，经映射表落地）。"""
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
