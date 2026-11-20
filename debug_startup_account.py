#!/usr/bin/env python3
"""
Debug script for startup_account switching.
Reads configuration from config file and tests SwitchAccount.switchAccount()
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from module.config.config import Config
from module.device.device import Device
from tasks.Component.SwitchAccount.switch_account import SwitchAccount
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from module.logger import logger


def main():
    # 配置文件名（不带 .json 后缀）
    # 可通过命令行参数指定：python debug_startup_account.py oas2
    config_name = sys.argv[1] if len(sys.argv) > 1 else 'oas1'

    logger.setLevel("DEBUG")
    logger.info(f"Loading config: {config_name}")

    config = Config(config_name)
    device = Device(config)

    # 从配置读取 startup_account
    cfg = config.restart.startup_account

    if not cfg.enable:
        logger.error("startup_account.enable is false in config, nothing to test")
        return

    if not cfg.is_valid():
        logger.error("startup_account config invalid: character and account must be non-empty")
        logger.error(f"  character='{cfg.character}' account='{cfg.account}'")
        return

    to = AccountInfo(
        character=cfg.character,
        svr=cfg.svr,
        account=cfg.account,
        account_alias=cfg.account_alias,
        apple_or_android=cfg.apple_or_android,
    )

    logger.info(f"Target account: {to.character} @ {to.svr} ({to.account})")

    sa = SwitchAccount(config, device, to)

    # 先检测当前页面
    from tasks.GameUi.page import page_main, page_login
    cur = sa.get_current_page()
    logger.info(f"Current page detected: {cur}")

    # 执行切号
    result = sa.switchAccount()
    logger.info(f"SwitchAccount result: {result}")

    if result:
        logger.info("✅ Startup account switch SUCCESS")
    else:
        logger.error("❌ Startup account switch FAILED")
        sys.exit(1)


if __name__ == '__main__':
    main()