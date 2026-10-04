"""灵猫边控联动模块：把气压传感器与官方边控会话接入输出设备闭环。

模块读取灵猫（BMTR）气压（0-60 kPa）与官方边控会话状态（DG-Lab 4.0 App
经 Socket V4 上报的 ``edgeState`` 0-4，核心控制页「边控状态」同款语义），
按配置模式跑闭环状态机，把目标强度经核心输入映射表派发给输出设备：

* ``sensor`` 气压闭环：到边阈值撤除刺激 → 冷静期满且气压回落恢复，往复边控；
* ``app`` 跟随官方边控会话：刺激/冷静/允许高潮四态驱动设备，可选释放开火；
* ``off`` 只提供映射变量（气压/到边标志/循环数…），映射表完全自定义。

META["config"] 声明全部配置项，宿主装载 config/margin_control.json 时自动
补齐缺省，联动页据此渲染映射表与模块设置；另有负鼠按键动作「灵猫气压
清零」「边控闭环暂停/恢复」。
"""

META = {
    "id": "margin_control",
    "name": "灵猫边控联动",
    "version": "0.1.0",
    "description": "灵猫气压 / 官方边控会话 → 输出设备闭环边控：按气压阈值"
                   "自动「刺激→到边→冷静→恢复」，或跟随 App 边控状态驱动"
                   "设备；映射表可完全自定义。",
    "settings_key": "margin_control",
    "default_enabled": False,
    "actions": ["margin_reset_pressure", "margin_guard_toggle"],
    "params": {
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
            "label": "刺激期强度", "type": "int",
            "default": 60, "min": 0, "max": 200,
            "group": "edge",
            "desc": "刺激期目标强度 0-200（刺激强度本身请在官方 App / "
                    "控制页调好波形与上限）",
        },
        "cool_strength": {
            "label": "冷静期强度", "type": "int",
            "default": 0, "min": 0, "max": 200,
            "group": "edge",
            "desc": "冷静期维持强度（0=完全撤除刺激）",
        },
        "release_strength": {
            "label": "释放期强度", "type": "int",
            "default": 0, "min": 0, "max": 200,
            "group": "edge",
            "desc": "App 模式「允许高潮」时维持强度（0=不干预）",
        },
        "ramp_s": {
            "label": "刺激爬升时长 (秒)", "type": "float",
            "default": 3.0, "min": 0.0, "max": 60.0, "step": 0.5,
            "group": "edge",
            "desc": "进入刺激期从 0 缓升到目标强度，0=立即",
        },
        "deny_zap_s": {
            "label": "到边惩罚脉冲 (秒)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 10.0, "step": 0.5,
            "group": "edge",
            "desc": "判定到边瞬间对目标设备双通道各来一次 N 秒瞬时脉冲，"
                    "0=关闭",
        },
        "release_fire_s": {
            "label": "释放开火时长 (秒)", "type": "float",
            "default": 0.0, "min": 0.0, "max": 60.0, "step": 0.5,
            "group": "edge",
            "desc": "进入「允许高潮」时定时开火 N 秒助飞，0=关闭（app 模式）",
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
        "output_slot": {
            "label": "目标输出设备 (slot_id)", "type": "str", "default": "",
            "group": "device",
            "desc": "惩罚脉冲/释放开火/停止归零的落点；映射表强度派发按"
                    "各家族第一台，绑定后同设备优先。留空自动选择",
        },
        # ---- 两张映射表之输入表（纯输入模块，无输出表） ----
        "mappings": {
            "label": "输入映射表", "type": "list", "default": [],
            "group": "map", "rows": "in",
            "desc": "行 {param: 核心输入参数, expr: 表达式}，表达式以 "
                    "{pressure} {drive} {phase} 等引用边控变量，可混合核心"
                    "输出参数，结果取整钳制后派发；表留空用默认行"
                    "（{drive} 驱动 A/B 强度）",
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
        """映射变量表（模块可写参数：气压 / 边控状态 / 闭环阶段等）。"""
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
        """暂停/恢复闭环（暂停即目标强度归零）。"""
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
