# This Python file uses the following encoding: utf-8
# @author kronus
# 配置轮换（配置实例轮换管理器）设置
# github https://github.com/CronusLM/OnmyojiAutoScript
from pydantic import BaseModel, Field


class Switcher(BaseModel):
    # 是否参与配置轮换：开启后，本配置会与其他开启轮换的配置互相切换运行
    # （当前配置遇到错误停止或空闲时切换到下一个参与轮换的配置）
    enable: bool = Field(default=False, description='switcher_enable_help')
    # 空闲切换冷却时间（分钟）：空闲触发的两次切换之间的最小间隔，防止来回切换；错误触发的切换不受限制
    idle_cooldown_minutes: int = Field(default=10, description='switcher_idle_cooldown_help')
