from adbutils import device

from module.config.config import Config
from module.device.device import Device
from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.Component.SwitchAccount.exit_game import ExitGame
from tasks.Component.SwitchAccount.login_account import LoginAccount
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_login
from tasks.base_task import BaseTask
from tasks.Restart.assets import RestartAssets
from tasks.Restart.login import LoginHandler

from module.logger import logger


class SwitchAccount(LoginAccount, ExitGame, GameUi, SwitchAccountAssets):

    def _at_login_page(self) -> bool:
        """当前是否停在登录页（「进入游戏」按钮页或账号选择页）。

        此时可直接就地切号（用户中心 → 切换用户 → 账号选择页），
        不必 goto_page(page_main) 先进庭院再退出。
        """
        # 已在庭院（庭院标志出现）：这次必须走 exitGame
        if self.appear(self.I_CHECK_MAIN):
            return False
        return bool(self.appear(self.I_CHECK_LOGIN_FORM)
                    or self.appear(self.I_SA_NETEASE_GAME_LOGO)
                    or self.ocr_appear(RestartAssets.O_LOGIN_ENTER_GAME_ORIGIN))

    def __init__(self, config: Config, device: Device, to: AccountInfo, frm: AccountInfo = None):
        """

        @param config:
        @type config:
        @param device:
        @type device:
        @param to: 要登录的账号信息
        @type to:
        @param frm: 上一个账号信息 ,避免关键字from
        @type frm:
        """
        super().__init__(config, device)
        self.to_account_info = to
        self.from_account_info = frm

    def switchAccount(self):
        logger.info("start switchAccount %s-%s", self.to_account_info.character, self.to_account_info.svr)
        # 判断所处界面
        curPage = self.get_current_page()

        # 已在登录页（例如冷启动停在「进入游戏」按钮页）时就地切号。
        # 绝不能 goto_page(page_main)：那会点「进入游戏」先进庭院，白跑一整趟。
        if curPage != page_login and curPage != page_main and self._at_login_page():
            logger.info("switchAccount: already at login page, switch in place")
            curPage = page_login

        if curPage != page_login and curPage != page_main:
            self.goto_page(page_main)
            curPage = self.get_current_page()
        if curPage == page_main:
            if not self.exitGame():
                logger.error("exit game failed, abort switch account")
                return False

        # 处于登录界面
        if not self.login(self.to_account_info):
            return False
        logger.info("%s login suc", self.to_account_info.character)
        # 处理位于登录界面各种奇葩弹窗
        login_handler = LoginHandler(config=self.config, device=self.device)
        login_handler.set_specific_usr(self.to_account_info.svr)
        login_handler.app_handle_login()

        return True


if __name__ == '__main__':
    config = Config('oas1')
    device=Device()
    toAccount=AccountInfo(account="email0@163.com", account_alias="emailO#emailo", apple_or_android=True, character="粘贴", svr="立秋夕烛")
    sa=SwitchAccount(config,device,toAccount)
    sa.switchAccount()