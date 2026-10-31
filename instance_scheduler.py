# This Python file uses the following encoding: utf-8
# 实例调度 CLI 入口：常驻元调度器，管理多个 OAS 实例的域级互斥开关。
#
# 用法（repo 根目录）：
#   python instance_scheduler.py                       # 默认域 default，10s 轮询
#   python instance_scheduler.py --domain mydomain     # 指定调度域
#   python instance_scheduler.py --interval 30 --port 22270
#
# 与 web server（python server.py）并存：server 托管实例进程时，本进程通过
# HostProbe（HTTP）探测/重启实例进程；纯 CLI 部署时进程探针退化为 pid 文件/设备探测。
import argparse
import time

from module.logger import logger
from module.instance_schedule.scheduler import (DEFAULT_POLL_INTERVAL,
                                                MIN_POLL_INTERVAL,
                                                InstanceScheduler)

# server 端口解析：args.port || WebuiPort || 22270（server.py:76 同款回退链）
def _resolve_default_port() -> int:
    try:
        from module.server.setting import State
        return int(State.deploy_config.WebuiPort) or 22270
    except Exception:
        return 22270


def main() -> None:
    parser = argparse.ArgumentParser(description='OAS 实例调度器（域级互斥元调度）')
    parser.add_argument('--domain', default='default', help='调度域名（同域互斥）')
    parser.add_argument('--interval', type=int, default=DEFAULT_POLL_INTERVAL,
                        help=f'轮询间隔秒数（≥{MIN_POLL_INTERVAL}）')
    parser.add_argument('--port', type=int, default=0,
                        help='web server 端口（0=自动：WebuiPort 回退 22270）')
    args = parser.parse_args()

    interval = max(MIN_POLL_INTERVAL, int(args.interval))
    port = args.port or _resolve_default_port()

    logger.hr(f'Instance scheduler: domain={args.domain}, interval={interval}s, port={port}', level=0)
    scheduler = InstanceScheduler(poll_interval=interval, server_port=port, domain=args.domain)

    try:
        while True:
            try:
                scheduler.poll_once()
            except Exception as e:
                # 单轮异常不终止守护循环（配置半截写盘/网络抖动等），下一轮重试
                logger.exception(f'[instance_schedule] poll error: {e}')
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info('[instance_schedule] instance scheduler exiting gracefully')
    except Exception as e:  # pragma: no cover
        logger.exception(e)
        raise


if __name__ == '__main__':
    main()
