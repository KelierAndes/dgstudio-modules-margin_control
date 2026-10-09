
META = {
    "id": "margin_control",
    "name": "灵猫边控联动",
    "version": "0.13.0",
    "description": "灵猫气压 / 官方边控会话 → 闭环边控（事件流数据面）："
                   "事件流把 BMTR.Pressure/EdgeState 写入模块变量，闭环判定后"
                   "发布刺激/惩罚强度等变量，设备动作由事件流的写入卡片驱动，"
                   "模块自身不下发设备命令；达限后的下一次到边即释放边（不惩罚、"
                   "刺激器=助力、循环置零）。",
    "settings_key": "margin_control",
    "default_enabled": False,
    "actions": ["margin_guard_toggle"],
    "params": {
        "pressure": {"label": "灵猫气压", "desc": "平滑 + 漏气补偿后气压 (kPa)",
                     "dir": "inout", "type": "Float"},
        "edge": {"label": "官方边控状态", "desc": "App 边控会话 0-4：0 停止 / "
                                                "1 刺激 / 2 冷静计时 / 3 冷静判定 / 4 允许高潮",
                 "dir": "inout", "type": "Int"},
        "stim_strength": {"label": "刺激器强度", "desc": "刺激期按爬升时长趋向"
                                                       "刺激强度，冷静期维持冷静强度，释放期输出"
                                                       "助力强度，其余 0（0-200）",
                          "dir": "in", "type": "Int"},
        "punish_strength": {"label": "惩罚器强度", "desc": "到边进冷静后的惩罚"
                                                         "输出，惩罚时长内非零（0-200）",
                            "dir": "in", "type": "Int"},
        "on_edge": {"label": "到边标志", "desc": "气压 ≥ 当前边缘阈值（自适应后"
                                                "红线）时为 1",
                    "dir": "in", "type": "Bool"},
        "on_release": {"label": "释放标志", "desc": "释放期（允许高潮，助力输出）"
                                                  "为 1，其余 0",
                       "dir": "in", "type": "Bool"},
        "cycles": {"label": "边控循环", "desc": "当前轮「到边→冷静」计数；释放完成"
                                              "后清零重新计",
                   "dir": "in", "type": "Int"},
    },
    "temps": [
        {"key": "pressure", "label": "灵猫气压读数", "dir": "inout",
         "type": "Float",
         "desc": "事件流的写入卡片把 BMTR.Pressure 写进 pressure 作为原始"
                 "气压 (kPa) 输入；模块读出的同名变量是平滑 + 漏气补偿后的值"},
        {"key": "edge", "label": "官方边控状态读数", "dir": "inout", "type": "Int",
         "desc": "事件流的写入卡片把 BMTR.EdgeState 写进 edge 的会话状态 "
                 "0-4（app 模式输入）"},
    ],
    "config": {
        "mode": {
            "label": "边控模式", "type": "choice",
            "choices": ["sensor", "app", "off"], "default": "sensor",
            "group": "basic",
            "desc": "sensor=按气压判定自动边控（不依赖 App）；"
                    "app=跟随官方 App 边控会话（Socket V4，状态 0-4）；"
                    "off=不闭环，仅提供变量数值",
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
        "stim_strength": {
            "label": "刺激强度", "type": "int",
            "default": 60, "min": 0, "max": 200,
            "group": "strength",
            "desc": "刺激期目标强度，经变量 {stim_strength} 落地"
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
            "desc": "释放期（允许高潮）输出强度；与刺激强度共用变量 "
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
            "desc": "判定到边的瞬间以该强度输出（经变量 "
                    "{punish_strength}），0=关闭惩罚",
        },
        "punish_s": {
            "label": "惩罚时长 (秒)", "type": "float",
            "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.5,
            "group": "strength",
            "desc": "惩罚输出的持续时长，0=关闭惩罚",
        },
        "adapt_stim": {
            "label": "刺激阶段红线自适应", "type": "bool",
            "default": False,
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
            "default": False,
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
    },
}

from plugins import ModuleBase, spec_defaults

from modules.margin_control import bridge as _bridge_mod
from modules.margin_control.bridge import (PARAM_DEFS, MarginBridge,
                                           MarginConfig)

MARGIN_CONFIG_DEFAULTS = spec_defaults(META["config"])

# 模块内派发层时代的设置项：换算与设备写入已全部交给宿主的「事件流」画布
_LEGACY_KEYS = ("events", "temps", "mappings", "outputs")


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
        return dict(META["config"])

    def link_params(self) -> list[dict]:
        """闭环发布 / 采样的变量：方向与值类型显式声明，宿主据此决定可否写入。"""
        return [{"name": name, "label": str(item.get("label") or name),
                 "dir": str(item.get("dir") or "in"),
                 "type": str(item.get("type") or "Float"),
                 "desc": str(item.get("desc") or "")}
                for name, item in META["params"].items()]

    def on_load(self, ctx) -> None:
        self.ctx = ctx
        drop_mapping_tables(ctx.settings, ctx.log)

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
            self.ctx.get_state,
            events=self.ctx.events,
        )
        self.bridge.log = self.ctx.log
        await self.bridge.start()

    async def reload_config(self) -> None:
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

    def button_actions(self) -> list:
        from plugins import ButtonAction

        return [
            ButtonAction("margin_guard_toggle", "边控闭环 暂停/恢复",
                         on_press=self._press_guard_toggle),
        ]

    def _press_guard_toggle(self, slot_id, argument) -> None:
        if self.ctx is None:
            return
        if self.bridge is None:
            self.ctx.log("边控闭环尚未启动（先在模块页启动模块）")
            return
        self.bridge.toggle_pause()


def drop_mapping_tables(settings: dict, log=None) -> bool:
    """清除模块派发层时代的设置项：接线改在「事件流」画布里完成。"""
    stale = [key for key in _LEGACY_KEYS if key in settings]
    if not stale:
        return False
    for key in stale:
        settings.pop(key, None)
    if log is not None:
        log("模块内的派发层已移除，遗留设置项（" + "、".join(stale) +
            "）已清除：气压 / 边控状态请用事件流的写入卡片写入 pressure / edge，"
            "刺激与惩罚强度请读 {stim_strength} {punish_strength} 变量驱动设备")
    return True

