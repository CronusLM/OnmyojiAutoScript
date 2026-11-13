# This Python file uses the following encoding: utf-8
# @author kronus
# 配置轮换（配置实例轮换管理器）设置
# github https://github.com/CronusLM/OnmyojiAutoScript
from pydantic import BaseModel, Field


class Switcher(BaseModel):
    # 是否参与配置轮换：开启后，本配置会与同实例分组内其他开启轮换的配置互相切换运行
    # （当前配置遇到错误停止或空闲时切换到组内下一个参与轮换的配置），
    # 同一实例分组内同时只运行一个配置，不同实例分组的配置可同时运行
    enable: bool = Field(default=False, description='switcher_enable_help')
    # 空闲切换冷却时间（分钟）：空闲触发的两次切换之间的最小间隔，防止来回切换；错误触发的切换不受限制
    idle_cooldown_minutes: int = Field(default=10, description='switcher_idle_cooldown_help')
    # 显式实例分组标识：标记本配置属于哪台云手机/模拟器实例（按设备分组并发的分组键）。
    # 非空时以它为准；留空则回退到 device.serial（再回退 emulatorinfo_name/handle）。
    # 同实例分组内的配置互斥轮换，不同实例分组的配置可同时运行。
    instance: str = Field(default='', description='switcher_instance_help')
