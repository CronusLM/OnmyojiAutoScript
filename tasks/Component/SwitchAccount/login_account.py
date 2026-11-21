from copy import deepcopy
import re
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
        # target 是 AccountInfo 对象，account_alias 里保存了 '#' 分隔的已知误识别形态
        aliases = [a for a in (target.account_alias or "").split("#") if a]
        return match_account(target.account, ocr_text, aliases=aliases)

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
                # 点击登录按钮（新版登录页没有"选择平台"弹窗，点即登录），
                # 点击后以"已不在账号选择页"作为成功判据，而不是再次判断 LOGO 是否在。
                login_clicked = False
                for attempt in range(3):
                    self.screenshot()
                    if not self.appear(self.I_SA_NETEASE_GAME_LOGO):
                        login_clicked = True
                        break
                    if self.click(self.I_SA_ACCOUNT_LOGIN_BTN, interval=2):
                        if not self.wait_until_appear(self.I_SA_NETEASE_GAME_LOGO, wait_time=10):
                            login_clicked = True
                            break
                    logger.warning("login btn click attempt %d/3 failed, retry...", attempt + 1)

                if not login_clicked:
                    logger.error("login btn click failed after 3 attempts")
                    return False

                isAccountLogon = True
                continue
            # 在游戏登录界面（非账号选择界面，无网易Logo，有进入游戏按钮）
            if not self.appear(self.I_SA_NETEASE_GAME_LOGO) and self.appear(self.I_CHECK_LOGIN_FORM):
                # 判断是否已经账号登录
                if not isAccountLogon:
                    # 新版本：首次点击用户中心直接跳转到账号选择界面；后续在登录界面点击切换账号按钮
                    # 规则 B：单击 + wait_until_appear + 重试，避免盲目 ui_click 死循环
                    if self.appear(self.I_SA_SWITCH_ACCOUNT_BTN):
                        # 弹窗已展开：点"切换用户"，等账号选择页
                        if not self._click_then_wait(self.I_SA_SWITCH_ACCOUNT_BTN, self.I_SA_NETEASE_GAME_LOGO,
                                                     interval=1, wait_time=10):
                            logger.warning("switch_account_btn -> logo failed")
                    else:
                        # 两步走：点用户中心 -> 等"切换用户"按钮出现 -> 再点它等账号选择页。
                        # 若直接等 LOGO，会反复点用户中心，把刚弹出的窗口又点关。
                        if self._click_then_wait(self.C_SA_LOGIN_FORM_USER_CENTER,
                                                 self.I_SA_SWITCH_ACCOUNT_BTN, interval=2, wait_time=6):
                            if not self._click_then_wait(self.I_SA_SWITCH_ACCOUNT_BTN, self.I_SA_NETEASE_GAME_LOGO,
                                                         interval=1, wait_time=10):
                                logger.warning("switch_account_btn -> logo failed")
                        else:
                            logger.warning("user_center -> switch_account_btn failed")
                    continue

                # 已登录账号：以服务器为准切换(同一账号在一个服务器只有一个角色)
                if not isCharacterSelected:
                    if not accountInfo.svr:
                        logger.error("svr is empty, cannot switch server")
                        break
                    logger.info("try to select svr %s", accountInfo.svr)
                    if not self.select_server(accountInfo.svr):
                        break
                    isCharacterSelected = True
                    # 弹层选中后已自动退回登录界面，下一轮点登录
                    continue

                # 服务器已选好
                if not self._enter_game():
                    logger.error("enter game failed, abort switch")
                    return False
                logger.info("Clicked enter game button")
                break

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

    # 规则 B：单击 + wait_until_appear + 重试（最多 3 次）
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
            matched, score = self._masked_account_match(text, accountInfo)
            logger.info("current account OCR text: %s (match=%.2f)", text, score)
            return matched
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

    # Substring hits are not enough: "上次登录1分钟前" / "上次登录11-15" are relative
    # time labels, "添加新账号" is the add-account entry. None of them is an account.
    # Only rows that look like a masked phone ("188****7062") or an email/alias count.
    _ACCOUNT_ROW_PATTERN = re.compile(r"\*|@")

    # I_SA_ACCOUNT_DROP_DOWN_CLOSED matches the white dropdown icon in BOTH
    # states (expanded / collapsed). Passing it as the `stop` of
    # ui_click_until_disappear never terminates, so the loop clicks it again
    # every `interval` seconds and closes the list it just opened.
    # Click once, wait for the animation, then decide by OCR text instead.
    # 1.8s was measured against a half-expanded list: the panel was still sliding
    # so the first OCR only ever saw the already-selected row. The expand
    # animation needs ~2.5s before every row is laid out at its final position.
    _DROPDOWN_OPEN_WAIT = 2.8

    def _list_items(self, ocr_list):
        """OCR the account list on the current screenshot."""
        return ocr_list.detect_and_ocr(self.device.image)

    def _list_rows(self, ocr_texts):
        """Rows that actually look like an account (masked phone or email)."""
        return [t.strip() for t in ocr_texts if t and self._ACCOUNT_ROW_PATTERN.search(t)]

    def _list_opened(self, ocr_texts) -> bool:
        """True when the dropdown is FULLY open, i.e. shows >= 2 account rows.

        A single masked row is not proof of an open list: while the panel is
        still animating in (or when it is collapsed) the selected account in
        the closed control is the only thing OCR sees. Treating that as open
        made selectAccount skip the wait and scroll on a half-rendered list,
        where two consecutive reads are identical -> "scrolled to bottom".
        """
        return len(self._list_rows(ocr_texts)) >= 2

    def _open_dropdown(self, ocr_list):
        """Click the dropdown once, wait for the animation, return OCR results."""
        self.click(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED)
        time.sleep(self._DROPDOWN_OPEN_WAIT)
        self.screenshot()
        return self._list_items(ocr_list)

    # The list renders asynchronously: the first OCR after opening often sees
    # only the previously selected row. Click on that snapshot and you select
    # the wrong account and lose the dropdown. Re-read until two consecutive
    # OCR passes agree (stable) before trusting the rows.
    _LIST_STABLE_RETRY = 3
    _LIST_STABLE_WAIT = 0.6

    def _stable_list_items(self, ocr_list):
        """Re-OCR the account list until two consecutive reads are identical."""
        ocrRes = self._list_items(ocr_list)
        texts = [item.ocr_text for item in ocrRes]
        for _ in range(self._LIST_STABLE_RETRY):
            time.sleep(self._LIST_STABLE_WAIT)
            self.screenshot()
            newRes = self._list_items(ocr_list)
            new_texts = [item.ocr_text for item in newRes]
            if new_texts == texts:
                return newRes
            ocrRes, texts = newRes, new_texts
        return ocrRes


    def selectAccount(self, accountInfo: AccountInfo):
        logger.info("start selectAccount")
        ocr_list = deepcopy(self.O_SA_ACCOUNT_ACCOUNT_LIST)
        for i in range(3):
            guard = Timer(30)
            guard.start()

            # Do not click the dropdown when the list is already open (it would close it).
            ocrRes = self._stable_list_items(ocr_list)
            ocr_texts = [item.ocr_text for item in ocrRes]
            if not self._list_opened(ocr_texts):
                ocrRes = self._open_dropdown(ocr_list)
                ocrRes = self._stable_list_items(ocr_list)
                ocr_texts = [item.ocr_text for item in ocrRes]
                if not self._list_opened(ocr_texts):
                    logger.warning("account list still not open after click, retry")
                    continue

            reached_bottom = False
            while 1:
                if guard.reached():
                    logger.warning("selectAccount guard Timer(30) reached")
                    break

                idx, score = find_best_match(accountInfo.account, ocr_texts, kind="account")
                if idx >= 0:
                    box = ocrRes[idx].box
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
                    # Re-read the selected row to confirm the switch really happened.
                    time.sleep(1.5)
                    self.screenshot()
                    if self._current_account_is(accountInfo):
                        logger.info("account [ %s ] found", accountInfo.account)
                        return True
                    logger.warning("clicked account [ %s ] (score=%.2f) but selection not confirmed",
                                   ocr_texts[idx], score)
                    break

                # Target not in the visible page: scroll once; unchanged text means bottom.
                self.swipe(self.S_SA_ACCOUNT_LIST_UP, 1.5)
                time.sleep(0.8)
                self.screenshot()
                new_ocrRes = self._list_items(ocr_list)
                new_texts = [item.ocr_text for item in new_ocrRes]
                if new_texts == ocr_texts:
                    logger.info("account list scrolled to bottom, [ %s ] not there", accountInfo.account)
                    reached_bottom = True
                    break
                ocrRes, ocr_texts = new_ocrRes, new_texts

            if reached_bottom or not ocr_texts:
                break

        logger.info("account [ %s ] not found ", accountInfo.account)
        return False

    def _svr_name_texts(self):
        """OCR 四个服务器选项卡的名称区，返回 [(tabIndex, text), ...]（读不到的跳过）。"""
        out = []
        for idx in range(1, 5):
            ocr = getattr(self, "O_SA_SELECT_SVR_NAME_%d" % idx)
            try:
                text = ocr.ocr_single_line(self.device.image)
            except Exception as e:
                logger.warning("svr name tab%d ocr failed: %s", idx, e)
                text = ""
            out.append((idx, (text or "").strip()))
        return out

    def select_server(self, svrName: str) -> bool:
        """在服务器选择界面点击目标服务器的选项卡。

        关键交互(实测)：点击选项卡即完成选服务器，界面会自动退回"服务器登录界面"，
        之后点一次登录即可进入该服务器；不存在"再点角色卡"这一步。
        同一账号在一个服务器只有一个角色，所以选对服务器 = 选对角色。

        @param svrName: 目标服务器名，如 "愿予必成"
        @return: True 已点到该服务器选项卡
        """
        if not svrName:
            logger.warning("select_server: svrName empty, nothing to do")
            return False

        # 1) 打开服务器选择界面
        if not self.appear(self.O_SA_CHECK_SELECT_SVR):
            self.click(self.C_SA_LOGIN_FORM_SWITCH_SVR_BTN, interval=1.5)
            time.sleep(self._DROPDOWN_OPEN_WAIT)

        # 2) 在四个选项卡的名称区里找目标服务器
        guard = Timer(15)
        guard.start()
        while 1:
            self.screenshot()
            tabs = self._svr_name_texts()
            logger.info("svr select tabs: %s", [(i, t) for i, t in tabs])

            best_idx, best_score = -1, 0.0
            for idx, text in tabs:
                if not text:
                    continue
                matched, score = match_name(svrName, text, kind="svr")
                if matched and score > best_score:
                    best_idx, best_score = idx, score

            if best_idx > 0:
                # 3) 点选项卡名称区中心（落在 305x120 可点击卡片内）
                ocr = getattr(self, "O_SA_SELECT_SVR_NAME_%d" % best_idx)
                self.click(ocr)
                logger.info("svr [ %s ] selected by tab %d (score=%.2f)", svrName, best_idx, best_score)
                return True
            if guard.reached():
                break
            time.sleep(1)

        logger.warning("svr [ %s ] not found in server tabs %s", svrName, tabs)
        return False


    def _enter_game(self, timeout: float = 90) -> bool:
        """点「进入游戏」（OCR 文字定位，同 Restart 任务）直到庭院页出现。

        只点一次并不代表登录成功，这里以 I_CHECK_MAIN 出现
        作为“真正进入游戏”判据；未出现就继续点。
        为何要在函数内局部 import：tasks.Restart.* 会反向引用本模块，
        顶层 import 会形成循环导入。
        """
        from tasks.Restart.assets import RestartAssets
        from tasks.GameUi.assets import GameUiAssets

        guard = Timer(timeout)
        guard.start()
        clicked = 0
        while 1:
            self.screenshot()
            if self.appear(GameUiAssets.I_CHECK_MAIN):
                logger.info("'enter game' confirmed: main page detected (%ds)", clicked)
                return True
            if guard.reached():
                logger.error("'enter game' timeout %ss, still not at main page", timeout)
                return False
            if (self.ocr_appear_click(RestartAssets.O_LOGIN_ENTER_GAME_ORIGIN, interval=3)
                    or self.ocr_appear_click(RestartAssets.O_LOGIN_ENTER_GAME, interval=3)):
                clicked += 1
                logger.info("'enter game' clicked %d time(s)", clicked)

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
