from copy import deepcopy
import time

import cv2

from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.Component.SwitchAccount.fuzzy_match import match_account, match_name, find_best_match
from tasks.base_task import BaseTask
from module.base.timer import Timer
from module.logger import logger
from module.atom.image import RuleImage
from module.atom.ocr import RuleOcr
from module.atom.click import RuleClick
from module.atom.gif import RuleGif
from module.atom.swipe import RuleSwipe


class LoginAccount(BaseTask, SwitchAccountAssets):

    # 灰度化流水线（上游 HEAD 同款）：BGR2GRAY -> 二值化 -> 反转 -> GRAY2RGB
    # 只作用于裁剪副本，绝不变异 self.device.image
    def _to_gray_rgb(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 100, 255, cv2.THRESH_BINARY)
        inv = cv2.absdiff(255, binary)
        return cv2.cvtColor(inv, cv2.COLOR_GRAY2RGB)

    # 掩码账号（153****4444）OCR 文本永远不等于完整账号字符串，
    # 必须裸读文本 + 模糊比对，不能用 ocr_appear(keyword)（SINGLE 精确匹配恒 False）。
    # 返回深拷贝 RuleOcr 实例，避免污染共享类属性。
    def _masked_account_match(self, ocr_text, target):
        matched, _ = match_account(target, ocr_text)
        return matched

    def login(self, accountInfo: AccountInfo) -> bool:
        """
        @param accountInfo:
        @type accountInfo:
        @return:    True    点击了"进入游戏"按钮
                    False   未找到相应角色
        @rtype:bool
        """
        self.screenshot()
        #
        if not (self.appear(self.I_CHECK_LOGIN_FORM) or self.appear(self.I_SA_NETEASE_GAME_LOGO)):
            logger.error("Unknown Page,%s %s Login Failed", accountInfo.character, accountInfo.svr)
            return False

        #
        isAccountLogon = False
        isCharacterSelected = False
        # 不再直接污染共享 RuleOcr 实例的 keyword；改在需要读文本处用深拷贝本地实例 + 模糊比对
        guard = Timer(180)
        guard.start()
        while 1:
            self.screenshot()
            if guard.reached():
                logger.warning("login() guard Timer(180) reached, abort")
                break
            # 处于 选择服务器界面 直接点击空白区域退出该界面 进入切换账号流程
            if self.appear(self.I_SA_CHECK_SELECT_SVR_1) or self.appear(self.I_SA_CHECK_SELECT_SVR_2):
                self.click(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT)
                continue

            # 处于选择账号界面（网易Logo可见）
            if self.appear(self.I_SA_NETEASE_GAME_LOGO):
                if not accountInfo.account:
                    logger.error("param account is None,cannot switch account")
                    return False
                # 当前选择账号不是目标account（掩码账号用模糊比对）
                if not self._current_account_is(accountInfo):
                    # 没有找到account，需要从列表中选择
                    if not self.selectAccount(accountInfo):
                        self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_ACCOUNT_CLOSE_BTN,
                                                      stop=self.I_CHECK_LOGIN_FORM, timeout=20)
                        return False
                    # selectAccount 后更新图片
                    self.screenshot()
                # 点击登录按钮，进入游戏登录界面（有进入游戏按钮）
                # 等待游戏登录界面出现（进入游戏按钮可见），这表示账号选择界面已关闭
                if not self.ui_click(self.C_SA_LOGIN_FORM_LOGIN_BTN, stop=self.I_CHECK_LOGIN_FORM, interval=1,
                                     timeout=15):
                    logger.warning("login btn click timeout, continue")
                isAccountLogon = True
                continue
            # 在游戏登录界面（非账号选择界面，无网易Logo，有进入游戏按钮）
            if not self.appear(self.I_SA_NETEASE_GAME_LOGO) and self.appear(self.I_CHECK_LOGIN_FORM):
                # 判断是否已经账号登录
                if not isAccountLogon:
                    # 新版本：首次点击用户中心直接跳转到账号选择界面；后续在登录界面点击切换账号按钮
                    # 规则 B：单击 + wait_until_appear + 重试，避免盲目 ui_click 死循环
                    if self.appear(self.I_SA_SWITCH_ACCOUNT_BTN):
                        if not self._click_then_wait(self.I_SA_SWITCH_ACCOUNT_BTN, self.I_SA_NETEASE_GAME_LOGO,
                                                     interval=1, wait_time=8):
                            logger.warning("switch_account_btn -> logo failed")
                    else:
                        if not self._click_then_wait(self.C_SA_LOGIN_FORM_USER_CENTER, self.I_SA_NETEASE_GAME_LOGO,
                                                     interval=1, wait_time=8):
                            logger.warning("user_center -> logo failed")
                    continue

                # 已登录账号，检查当前显示的角色/服务器是否匹配
                # 尝试通过 OCR 识别当前角色名（可能需要展开服务器列表）
                if not isCharacterSelected:
                    # 先尝试点击切换服务器按钮进入服务器选择界面
                    if self.switch_character(accountInfo.character):
                        isCharacterSelected = True
                        continue
                    # 如果 switch_character 返回 False，可能是当前服务器列表没有该角色
                    # 尝试通过 switch_svr 切换服务器
                    if accountInfo.svr and accountInfo.svr != "":
                        logger.info("try to find character with svrName %s", accountInfo.svr)
                        isCharacterSelected = self.switch_svr(accountInfo.svr)
                        if isCharacterSelected:
                            continue
                    break

                # 角色已选中，点击进入游戏
                self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_ENTER_GAME_BTN, stop=self.I_CHECK_LOGIN_FORM,
                                              interval=1, timeout=20)
                logger.info("Clicked enter game button")
                break
            continue

        # 切换角色失败 /未找到该角色
        # 尝试使用 选择服务器方式
        if isAccountLogon and not isCharacterSelected and accountInfo.svr is not None and accountInfo.svr != "":
            logger.info("try to find character with svrName %s", accountInfo.svr)
            isCharacterSelected = self.switch_svr(accountInfo.svr)
        if isAccountLogon and isCharacterSelected:
            # 成功登录账号 找到角色
            logger.info("character %s-%s account:%s %s login Success", accountInfo.character, accountInfo.svr,
                        accountInfo.account,
                        'Android' if accountInfo.apple_or_android else 'Apple')
            return True

        logger.error("character %s-%s account:%s %s login Failed", accountInfo.character, accountInfo.svr,
                     accountInfo.account,
                     'Android' if accountInfo.apple_or_android else 'Apple')
        return False

    # 规则 B：单击 + wait_until_appear + 重试（≤2 次）
    def _click_then_wait(self, click, target, interval=1, wait_time=8):
        for attempt in range(3):
            self.screenshot()
            if self.appear(target):
                return True
            if not self.click(click, interval=interval):
                logger.warning("click %s skipped (interval not reached) attempt=%d", getattr(click, 'name', click),
                               attempt)
            if self.wait_until_appear(target, skip_first_screenshot=True, wait_time=wait_time):
                return True
        return False

    # 读取账号选择界面当前选中账号文本，与目标模糊比对
    def _current_account_is(self, accountInfo):
        try:
            ocr = deepcopy(self.O_SA_ACCOUNT_ACCOUNT_SELECTED)
            text = ocr.ocr(self.device.image)
            if not text:
                logger.info("current account OCR empty")
                return False
            logger.info("current account OCR text: %s", text)
            return self._masked_account_match(text, accountInfo.account)
        except Exception as e:
            logger.warning("current_account_is exception: %s", e)
            return False

    def switch_svr(self, svrName: str):
        """
            点击切换服务器按钮，检测选择服务器界面
        @param svrName:
        @return:
        """
        logger.info("start switch_svr")
        self.ui_click(self.C_SA_LOGIN_FORM_SWITCH_SVR_BTN, self.I_SA_CHECK_SELECT_SVR_1, 1.5)
        # 展开底部角色列表,显示角色所属服务器
        self.screenshot()
        if self.appear(self.I_SA_CHECK_SELECT_SVR_1) and (not self.appear(self.I_SA_CHECK_SELECT_SVR_2)):
            logger.info("open svr icon")
            self.click(self.C_SA_SELECT_SVR_CHARACTER_LIST, interval=1.5)
            self.wait_until_appear(self.I_SA_CHECK_SELECT_SVR_2, False, 1)
            self.screenshot()

        # 深拷贝本地实例，避免污染共享 keyword
        ocr = deepcopy(self.O_SA_CHECK_SELECT_SVR)
        lastOcrList = []
        guard = Timer(60)
        guard.start()
        for page in range(20):
            self.screenshot()
            if guard.reached():
                logger.warning("switch_svr guard Timer(60) reached")
                break
            # 在裁剪副本上灰度化，提升小字号服务器名识别率
            gray_img = self._to_gray_rgb(self.device.image)
            ocrRes = ocr.detect_and_ocr(gray_img)
            found = False
            for ocrResItem in ocrRes:
                # 服务器名模糊比对（相似度阈值 0.6）
                m_name, s_name = match_name(svrName, ocrResItem.ocr_text, kind='svr')
                if m_name and s_name >= 0.6:
                    # box 为 ROI 相对坐标，点击前加 roi 偏移
                    box = ocrResItem.box
                    cx = ocr.roi[0] + int((box[0][0] + box[2][0]) / 2)
                    cy = ocr.roi[1] + int((box[0][1] + box[1][1]) / 2)
                    self.click((cx, cy))
                    logger.info("svr %s found at (%d,%d)", svrName, cx, cy)
                    found = True
                    break
            if found:
                return True
            curList = [r.ocr_text for r in ocrRes]
            if lastOcrList == curList:
                logger.info("svr list unchanged, stop scrolling")
                break
            lastOcrList = curList
            self.swipe(self.S_SA_SVR_SWIPE_LEFT)
            time.sleep(1.5)
            self.screenshot()

        # 没找到 关闭选择服务器界面
        self.click(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT)
        return False

    def switch_character(self, characterName: str):
        """
              需保证账号已登录 且处于登录界面
        @param characterName:
        @return:
        @rtype:
        """
        logger.info("start switch_character")
        # 改成对比是否出现 已有角色（图像 stop）
        self.ui_click(self.C_SA_LOGIN_FORM_SWITCH_SVR_BTN, self.O_SA_CHECK_SELECT_SVR)
        # 展开底部角色列表,显示角色所属服务器
        self.screenshot()
        open_guard = Timer(10)
        open_guard.start()
        while (not self.appear(self.I_SA_CHECK_SELECT_SVR_2)) and self.appear(self.I_SA_CHECK_SELECT_SVR_1):
            if open_guard.reached():
                logger.warning("open svr icon guard Timer(10) reached")
                break
            logger.info("open svr icon")
            self.click(self.C_SA_SELECT_SVR_CHARACTER_LIST, interval=1.5)
            self.wait_until_appear(self.I_SA_CHECK_SELECT_SVR_2, False, 1)
            self.screenshot()

        ocr = deepcopy(self.O_SA_SELECT_SVR_CHARACTER_LIST)
        lastCharacterNameList = []
        guard = Timer(60)
        guard.start()
        while 1:
            self.screenshot()
            if guard.reached():
                logger.warning("switch_character guard Timer(60) reached")
                break
            ocrRes = ocr.detect_and_ocr(self.device.image)
            # 去除角色等级数字
            characterNameList = [ocrResItem.ocr_text.lstrip('1234567890 ([<>])【】（）《》') for ocrResItem in ocrRes]
            logger.info(characterNameList)
            ocrResBoxList = [ocrResItem.box for ocrResItem in ocrRes]
            for index, item in enumerate(characterNameList):
                # 相似度匹配（>=0.75），兼容 OCR 误识别
                matched, score = match_name(characterName, item, kind='character')
                if not (matched and score >= 0.75):
                    continue
                box = ocrResBoxList[index]
                # box 为 ROI 相对坐标，加 roi 偏移；再减去 svr 图标偏移
                tmpClick = RuleClick(
                    roi_back=deepcopy(ocr.roi),
                    roi_front=[
                        ocr.roi[0] + int(box[0][0]),
                        ocr.roi[1] + int(box[0][1]) - 30,
                        int(box[1][0] - box[0][0]),
                        int(box[2][1] - box[1][1])],
                    name="tmpClick"
                )
                self.ui_click_until_disappear(tmpClick, stop=self.I_SA_CHECK_SELECT_SVR_2,
                                              interval=3, timeout=25)
                logger.info("character %s found,and clicked svr icon", characterName)
                return True
            if lastCharacterNameList == characterNameList:
                break
            logger.info(f'{characterName} not found,start swipe')
            lastCharacterNameList = characterNameList
            self.swipe(self.S_SA_ACCOUNT_LIST_UP)
            # 等待滑动动画完成
            time.sleep(1.5)
            self.screenshot()

        # 没找到 点击空白区域关闭选择服务器界面
        self.click(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT)
        return False

    def selectAccount(self, accountInfo: AccountInfo):
        logger.info("start selectAccount")
        # 不再污染共享 keyword；改用深拷贝实例 + 模糊匹配
        ocr_list = deepcopy(self.O_SA_ACCOUNT_ACCOUNT_LIST)
        # 正常情况一次就行,但防不住OCR搞幺蛾子 保险起见 多来几次吧 反正挂机不差这点
        for i in range(3):
            guard = Timer(30)
            guard.start()
            while 1:
                self.screenshot()
                if guard.reached():
                    logger.warning("selectAccount guard Timer(30) reached")
                    break
                if self.appear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED):
                    # 已选中的账号与目标模糊比对（掩码兼容）
                    if self._current_account_is(accountInfo):
                        return True
                    self.ui_click_until_disappear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED,
                                                  interval=1.5, timeout=20)
                    continue

                # 账号列表已打开状态
                ocrRes = ocr_list.detect_and_ocr(self.device.image)
                ocr_texts = [ocrResItem.ocr_text for ocrResItem in ocrRes]

                # 在列表中模糊查找目标账号（含掩码/email 误识别）
                idx = find_best_match(accountInfo.account, ocr_texts, kind='account')
                if idx >= 0:
                    box = ocrRes[idx].box
                    # box 为 ROI 相对坐标，加 roi 偏移后构造独立 RuleClick，不变异共享 .area
                    tmpClick = RuleClick(
                        roi_back=deepcopy(ocr_list.roi),
                        roi_front=[
                            ocr_list.roi[0] + int(box[0][0]),
                            ocr_list.roi[1] + int(box[0][1]),
                            int(box[1][0] - box[0][0]),
                            int(box[2][1] - box[1][1])],
                        name="accountTmpClick"
                    )
                    time.sleep(1)
                    self.click(tmpClick)
                    logger.info("account [ %s ] found", accountInfo.account)
                    return True

                # 未找到该账号
                if self.appear(self.I_SA_ACCOUNT_DROP_DOWN_ADD_ACCOUNT):
                    break
                self.swipe(self.S_SA_ACCOUNT_LIST_UP, 1.5)
                time.sleep(0.5)
        logger.info("account [ %s ] not found ", accountInfo.account)
        return False

    def jump2SelectAccount(self):
        """
            跳转到切换账号页面（账号选择界面，有网易Logo和红色登录按钮）
        @return:
        @rtype:
        """
        guard = Timer(60)
        guard.start()
        while 1:
            self.screenshot()
            if guard.reached():
                logger.warning("jump2SelectAccount guard Timer(60) reached")
                return
            # 已经在账号选择界面
            if self.appear(self.I_SA_NETEASE_GAME_LOGO) and self.appear(self.I_SA_ACCOUNT_LOGIN_BTN):
                return
            # 新版本：登录界面直接有切换账号按钮，点击即进入账号选择界面
            if self.appear(self.I_SA_SWITCH_ACCOUNT_BTN):
                self.ui_click(self.I_SA_SWITCH_ACCOUNT_BTN, self.I_SA_NETEASE_GAME_LOGO, interval=1.5, timeout=15)
                continue
            # 在登录界面但没有切换账号按钮（首次进入），点击用户中心
            if self.appear(self.I_CHECK_LOGIN_FORM):
                self.ui_click(self.C_SA_LOGIN_FORM_USER_CENTER, self.I_SA_NETEASE_GAME_LOGO, interval=1.5, timeout=15)
                continue

    def ui_click_until_disappear(self, click, interval: float = 1, stop: RuleImage | RuleGif = None,
                                 max_pass: int = None, timeout: float = None):
        """
        重写原ui_click_until_disappear方法,增加stop参数
        点击一个按钮直到stop消失
        如果click为RuleOcr ,直接当作RuleClick点击,不会进行ocr识别,
        @param interval:
        @param click:
        @param stop:
        @param max_pass: 最大循环/点击次数兜底（防止死循环）
        @param timeout: 超时兜底（秒）
        @return:
        """
        if (isinstance(click, RuleImage) or isinstance(click, RuleGif)) and (stop is None):
            stop = click
        if max_pass is None:
            max_pass = 8 if timeout is None else int(timeout / interval) + 1
        guard = Timer(25 if timeout is None else timeout)
        guard.start()
        while 1:
            if max_pass is not None:
                if max_pass <= 0:
                    logger.warning("ui_click_until_disappear max_pass exhausted, stop=%s", getattr(stop, 'name', stop))
                    break
                max_pass -= 1
            if guard.reached():
                logger.warning("ui_click_until_disappear timeout reached, stop=%s", getattr(stop, 'name', stop))
                break
            self.screenshot()
            if not self.appear(stop):
                break
            if isinstance(click, RuleImage) or isinstance(click, RuleGif):
                self.appear_then_click(click, interval=interval)
                continue
            elif isinstance(click, RuleClick):
                self.click(click, interval)
                continue
            elif isinstance(click, RuleOcr):
                self.click(click)
                continue
