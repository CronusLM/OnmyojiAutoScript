# This Python file uses the following encoding: utf-8
# 实例调度域声明（实例调度任务 T15）
from enum import Enum

from pydantic import BaseModel, Field

from tasks.Component.config_base import Time


class OnErrorAction(str, Enum):
    """实例出错时的处置动作（对应实例调度的错误切换路径）。"""

    NONE = 'none'                          # 不处理：保持现状，不关游戏也不切换
    CLOSE_GAME = 'close_game'              # 只关游戏：关闭出错实例的游戏，不关闭实例
    CLOSE_AND_SWITCH = 'close_and_switch'  # 关游戏并关实例：关闭出错实例后切换到域内下一个实例


class InstanceSchedule(BaseModel):
    """实例调度域声明：本配置（=一个 OAS 实例，一份 config/*.json）加入哪个互斥调度域。

    同一 domain 的多份配置构成一个互斥域：域内按各配置任务的 next_run 选最近者
    打开、关闭其他实例。多开账号隔离的设备信息（模拟器 serial / 桌面 profile_dir）
    仍以 device 组与 desktop 组为准，本组只声明调度行为。
    """

    # 参与实例调度：False 时本实例不参与域内互斥与切换，行为与现状一致
    enable: bool = Field(default=False, description='instance_schedule_enable_help')
    # 域名：同域互斥（同域名才参与统一调度），默认域 default
    domain: str = Field(default='default', description='instance_schedule_domain_help')
    # 实例形态：auto=按 device/desktop 配置自动判定，可选 emulator/desktop/cloud_phone
    instance_type: str = Field(default='auto', description='instance_schedule_type_help')
    # 实例出错时的动作，见 OnErrorAction
    on_error: OnErrorAction = Field(default=OnErrorAction.CLOSE_AND_SWITCH,
                                    description='instance_schedule_on_error_help')
    # 切换时关闭域内其他实例（用户语义=全关除目标）；关闭后仅保留目标实例运行
    close_others_on_switch: bool = Field(default=True, description='instance_schedule_close_others_help')
    # 防抖阈值：两次切换之间的最小时间间隔，防止域内实例 next_run 接近时来回振荡开关
    switch_threshold: Time = Field(default=Time(minute=5),
                                   description='instance_schedule_threshold_help')
