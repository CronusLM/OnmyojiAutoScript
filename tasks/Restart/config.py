# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import string

from pydantic import BaseModel, Field

from tasks.Restart.config_scheduler import RestartScheduler
from tasks.Component.config_base import ConfigBase, DateTime, MultiLine


class HarvestConfig(BaseModel):
    # 默认启用
    enable: bool = Field(default=True, description='harvest_enable_help')
    # 庭院事务
    enable_courtyard_affairs: bool = Field(default=True)
    # 永久勾玉卡
    enable_jade: bool = Field(default=True)
    # 签到
    enable_sign: bool = Field(default=True)
    # 999天的签到福袋
    enable_sign_999: bool = Field(default=True)
    # 邮件
    enable_mail: bool = Field(default=True)
    # 御魂加成
    enable_soul: bool = Field(default=True)
    # 体力
    enable_ap: bool = Field(default=True)


class TasksReset(BaseModel):
    reset_task_datetime_enable: bool = Field(default=False, description='reset_task_datetime_enable_help')
    reset_task_datetime: DateTime = Field(default="2023-01-01 00:00:00", description='rest_task_datetime_help')


class LoginCharacterConfig(BaseModel):
    # 同账号同服务器多个角色时,需要登录的角色名/服务器名
    character: str = Field(default="")


class StartupAccountConfig(BaseModel):
    """
    启动时自动切换的账号配置（每个 script config 固定登录一个小号）。
    平台统一为安卓（apple_or_android=True）。
    account_alias: OCR 容错别名，多个用 # 分隔，可后续再填。
    """
    # 是否在启动时切换到该账号
    enable: bool = Field(default=False, description='startup_account_enable_help')
    # 角色名字
    character: str = Field(default="", description='startup_account_character_help')
    # 角色所在服务器
    svr: str = Field(default="", description='startup_account_svr_help')
    # 网易账号(手机号/邮箱)
    account: str = Field(default="", description='startup_account_account_help')
    # OCR 容错别名，多个用 # 分隔
    account_alias: str = Field(default="", description='startup_account_account_alias_help')
    # 角色所属平台 安卓/苹果（默认安卓）
    apple_or_android: bool = Field(default=True, description='startup_account_apple_or_android_help')

    def is_valid(self) -> bool:
        return self.enable and self.character != "" and self.account != ""


class Restart(ConfigBase):
    scheduler: RestartScheduler = Field(default_factory=RestartScheduler)
    tasks_config_reset: TasksReset = Field(default_factory=TasksReset)
    harvest_config: HarvestConfig = Field(default_factory=HarvestConfig)
    login_character_config: LoginCharacterConfig = Field(default_factory=LoginCharacterConfig)
    startup_account: StartupAccountConfig = Field(default_factory=StartupAccountConfig)