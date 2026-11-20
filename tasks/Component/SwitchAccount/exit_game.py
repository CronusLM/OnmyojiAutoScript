from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.base_task import BaseTask
from module.logger import logger

# 本地 pad + 光速虚拟机（ADB 直连，低延迟），无需云手机的 3s 间隔保护
EXIT_GAME_INTERVAL = 2.5
# 每一步页面转换的等待上限（秒）。必须显著小于 device 的 60s stuck 窗口
# （stuck 计时从上一次真实点击起算，含进入本步之前的耗时），否则 stuck
# 检测会抢先抛 GameStuckError，走不到本模块的优雅失败路径。
EXIT_GAME_STEP_TIMEOUT = 25


class ExitGame(BaseTask, SwitchAccountAssets):
    """从游戏内（庭院）退出当前账号，回到登录相关的账号选择界面。

    职责边界：只负责「从游戏内退到登录界面」这一段页面转换：
        庭院（page_main） -> 设置界面 -> 用户中心弹窗 -> 账号选择界面（网易 LOGO）
    账号/角色选择、登录进游戏等“切号”逻辑由 LoginAccount.login() 负责，
    本组件不做任何账号信息判断。

    每一步都用「点击 + 目标页面判据」的方式推进（ui_click），带超时；
    任一步超时都打 error 日志并返回 False，绝不盲点后假装成功。
    """

    def exitGame(self) -> bool:
        """
        @return:    True    已退到账号选择界面（登录相关页面，login() 可直接接手）
                    False   任一步页面转换超时/未出现判据，未完成退出
        @rtype: bool
        """
        logger.info("start game exit")

        # 每步都拥有完整的 60s stuck 余量：清零 stuck 计时器，
        # 避免进入本流程前的导航耗时（goto_page 等）侵占无点击窗口，
        # 导致 device 的 stuck 检测抢先抛 GameStuckError。
        self.device.stuck_record_clear()

        # 1) 庭院左上角头像 -> 设置界面
        # 判据：设置界面顶部「设置」字样（I_SA_USER_CENTER_PROFILE）
        if not self.ui_click(self.C_SA_EG_PROFILE_PHOTO, self.I_SA_USER_CENTER_PROFILE,
                             interval=EXIT_GAME_INTERVAL, timeout=EXIT_GAME_STEP_TIMEOUT):
            logger.error("exitGame: page_main->settings 失败，点击头像后等待设置界面 %ss 超时",
                         EXIT_GAME_STEP_TIMEOUT)
            return False
        logger.info("exitGame: page_main->settings ok")

        # 2) 设置界面左侧「用户中心」 -> 用户中心弹窗
        # 判据：弹窗内「切换用户」按钮出现（I_SA_SWITCH_ACCOUNT_BTN）
        if not self.ui_click(self.I_SA_USER_CENTER, self.I_SA_SWITCH_ACCOUNT_BTN,
                             interval=EXIT_GAME_INTERVAL, timeout=EXIT_GAME_STEP_TIMEOUT):
            # 降级路径：用户中心弹窗可能被残留弹层挡住/误关，
            # 先尽力关闭弹窗再整体重试本步一次
            logger.warning("exitGame: settings->user_center 首次超时，尝试关闭弹窗后重试")
            self.ui_click(self.C_SA_LOGIN_FORM_USER_CENTER_CLOSE_BTN,
                          interval=1, timeout=5)
            if not self.ui_click(self.I_SA_USER_CENTER, self.I_SA_SWITCH_ACCOUNT_BTN,
                                 interval=EXIT_GAME_INTERVAL, timeout=EXIT_GAME_STEP_TIMEOUT):
                logger.error("exitGame: settings->user_center 失败，重试后等待切换用户按钮 %ss 仍超时",
                             EXIT_GAME_STEP_TIMEOUT)
                return False
        logger.info("exitGame: settings->user_center ok")

        # 3) 点击「切换用户」 -> 账号选择界面（网易 LOGO）
        # 判据：账号选择界面顶部网易游戏 LOGO（I_SA_NETEASE_GAME_LOGO），
        # 该页面是 login() 明确接受的两种入口页面之一，后续选号/登录由 login() 处理。
        if not self.ui_click(self.I_SA_SWITCH_ACCOUNT_BTN, self.I_SA_NETEASE_GAME_LOGO,
                             interval=EXIT_GAME_INTERVAL, timeout=EXIT_GAME_STEP_TIMEOUT):
            logger.error("exitGame: user_center->account_select 失败，等待网易 LOGO 界面 %ss 超时",
                         EXIT_GAME_STEP_TIMEOUT)
            return False
        logger.info("exitGame: user_center->account_select ok, page is account select (login related)")

        logger.info("exitGame: game exit success, now at account select page")
        return True
