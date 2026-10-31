# This Python file uses the following encoding: utf-8
# 元调度器核心：实例调度域的成员聚合、三态判据、切换决策与执行、自愈与收敛。
#
# 设计依据：T10 方案 v3 + 用户 6 条澄清（任务板 task-10/task-14）。
# 职责边界（见 __init__.py）：
#   - 决策数据 = 各 config/*.json 只读快照（read_file 的 FileLock 快照，module/config/utils.py:33-70），
#     全程不实例化 ConfigModel——避免 pydantic 校验开销与 ConfigModel.__setattr__ 自动落盘风险
#     （module/config/config_model.py:170-179）。
#   - 实例设备开关 = 直调 module/device/platform2 的 CLI/psutil 能力（不构造 Device、
#     不依赖本进程 ADB 连接）。PlatformWindows() 无参构造安全（PlatformBase 对无 config
#     的调用方有 hasattr 兜底，platform_base.py:36/64），已实测。
#   - 跨实例互斥 = 元调度器单实例常驻轮询 + switch_threshold 防抖；不写 queue_state.json，
#     queue_mode 归实例进程自治（域内启用元调度器时建议 queue_mode=False，见双主警告）。
#
# 与实例进程（T16 script.py 钩子）的分工：
#   - 实例进程到点后自行拉起游戏（Restart 改写分支，script.py:427-431 禁用空闲预热自主拉起）
#   - 元调度器负责域级协调：关非目标实例设备、开目标实例设备、全关自愈、违规多开收敛
import os
import time
import ctypes
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

from module.logger import logger
from module.config.utils import read_file

# 轮询间隔下限（本仓 interval/sleep ≥3 铁律）与默认值
MIN_POLL_INTERVAL = 3
DEFAULT_POLL_INTERVAL = 10

# next_run 的持久化格式（module/config/config.py 写盘惯例 str("%Y-%m-%d %H:%M:%S")）
NEXT_RUN_FORMAT = '%Y-%m-%d %H:%M:%S'

# config/*.json 里非任务组的保留键（聚合 next_run 时跳过）
_NON_TASK_KEYS = {'config_name', 'running_task', 'script'}


class InstanceState(Enum):
    """实例三态（T10 判据 v2）：死亡=进程探测死亡；空闲=存活且无事可做；运行中=不动作。"""

    DEAD = 'dead'        # 实例进程死亡（设备可能还开着——错误切换场景）
    IDLE = 'idle'        # 存活 + running_task 空 + 无 pending 任务
    RUNNING = 'running'  # 存活 + 自报 running_task 非空或有到期任务


class ProcessProbe:
    """托管形态进程探针：直接持有 multiprocessing.Process 引用（同进程内嵌元调度器时用）。"""

    def __init__(self, process=None):
        self.process = process

    def is_alive(self):
        """返回 True/False；process 未知时返回 None（未知 ≠ 死亡，不触发错误切换）。"""
        if self.process is None:
            return None
        try:
            return bool(self.process.is_alive())
        except Exception as e:
            logger.warning(f'[instance_schedule] process probe failed: {e}')
            return None

    @property
    def exitcode(self):
        if self.process is None:
            return None
        try:
            return self.process.exitcode
        except Exception:
            return None

    def restart(self) -> bool:
        """托管形态可重启：stop + start（module/server/script_process.py:50-64 语义）。"""
        try:
            if self.process is not None and self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=0.7)
                if self.process.is_alive():
                    self.process.kill()
            self.process.start()
            return True
        except Exception as e:
            logger.warning(f'[instance_schedule] process restart failed: {e}')
            return False


class PidFileProbe:
    """CLI 形态进程探针：从 pid 文件读 PID 后用 psutil 判活。

    pid 文件由实例进程（或部署方）落盘；文件缺失/损坏按未知处理（None），
    由设备级探测兜底，绝不把「读不到」当成「已死亡」。
    """

    def __init__(self, pid_file):
        self.pid_file = str(pid_file) if pid_file else None

    def _read_pid(self):
        if not self.pid_file or not os.path.isfile(self.pid_file):
            return None
        try:
            with open(self.pid_file, 'r', encoding='utf-8') as f:
                return int(str(f.read()).strip())
        except Exception as e:
            logger.warning(f'[instance_schedule] read pid file failed: {self.pid_file}: {e}')
            return None

    def is_alive(self):
        pid = self._read_pid()
        if pid is None:
            return None
        try:
            import psutil
            return psutil.pid_exists(pid)
        except Exception as e:
            logger.warning(f'[instance_schedule] pid probe failed: {e}')
            return None

    def restart(self) -> bool:
        # CLI 形态的实例进程由用户/部署方拉起，元调度器无权代拉
        return False


class HostProbe:
    """web server 托管形态探针：走本机 server HTTP API（路由无前缀挂根，module/server/app.py:44-45）。

    判活局限（推断，已在 T14 记录）：`GET /{name}/state` 是 SSE 流（script_router.py:172-185，
    挂起不返回），REST 无实例进程只读状态端点——is_alive() 只能确认 server 可达，实例进程
    存活返回 None（未知），由设备级探测兜底；restart() 用现成的 GET stop + GET start
    （script_router.py:105-118）。
    """

    def __init__(self, script_name: str, port: int = 22270, timeout: int = 3):
        self.script_name = script_name
        self.port = port
        self.timeout = timeout

    def _get(self, path: str):
        import requests
        return requests.get(f'http://127.0.0.1:{self.port}{path}', timeout=self.timeout)

    def is_alive(self):
        try:
            return self._get('/config_list').status_code == 200
        except Exception:
            return None

    def restart(self) -> bool:
        """疑似调度死时先重启实例进程（R1 定稿：restart 优先于错误切换）。"""
        try:
            self._get(f'/{self.script_name}/stop')
            # stop 是异步 terminate+join(0.7s)+kill，给一拍缓冲（≥3 合规）
            time.sleep(3)
            self._get(f'/{self.script_name}/start')
            logger.info(f'[instance_schedule] host restart issued: {self.script_name}')
            return True
        except Exception as e:
            logger.warning(f'[instance_schedule] host restart failed: {self.script_name}: {e}')
            return False


class InstanceInfo:
    """域成员只读快照：一份 config/*.json 的调度相关信息（不含任何可变句柄）。"""

    def __init__(self, name: str, path: str, raw: dict):
        self.name = name                       # config_name（实例主键）
        self.path = path                       # config json 绝对路径
        self.raw = raw                         # 原始 json dict（只读，勿改）

        schedule = raw.get('script', {}).get('instance_schedule', {}) or {}
        self.enable = bool(schedule.get('enable', False))
        self.domain = str(schedule.get('domain', 'default'))
        self.instance_type = str(schedule.get('instance_type', 'auto'))
        self.on_error = str(schedule.get('on_error', 'close_and_switch'))
        self.close_others_on_switch = bool(schedule.get('close_others_on_switch', True))
        self.switch_threshold = self._time_to_seconds(schedule.get('switch_threshold', '00:05:00'))

        device = raw.get('script', {}).get('device', {}) or {}
        self.serial = str(device.get('serial', 'auto'))
        self.emulatorinfo_type = str(device.get('emulatorinfo_type', 'auto'))
        self.emulatorinfo_name = str(device.get('emulatorinfo_name', ''))
        self.emulatorinfo_path = str(device.get('emulatorinfo_path', ''))
        self.package_name = str(device.get('package_name', 'auto'))

        desktop = raw.get('script', {}).get('desktop', {}) or {}
        self.desktop_enable = bool(desktop.get('enable', False))
        self.desktop_game_path = str(desktop.get('game_path', ''))
        self.desktop_profile_dir = str(desktop.get('profile_dir', ''))
        self.desktop_window_title = str(desktop.get('window_title', ''))

        error = raw.get('script', {}).get('error', {}) or {}
        self.notify_config = str(error.get('notify_config', 'provider: null'))
        self.notify_enable = bool(error.get('notify_enable', False))

        self.running_task = str(raw.get('running_task', '') or '')

        # 实际形态：instance_type=auto 时从 device/desktop 组判据推导（T9 矩阵）
        self.resolved_type = self.instance_type
        if self.instance_type == 'auto':
            if self.desktop_enable:
                self.resolved_type = 'desktop'
            elif self.emulatorinfo_type == 'CloudPhone':
                self.resolved_type = 'cloud_phone'
            else:
                self.resolved_type = 'emulator'

        # next_run 快照（域内最早到期任务）与 pending 计数（已到期未跑任务数）
        self.next_run, self.pending_count = self._compute_schedule(raw)

    @staticmethod
    def _time_to_seconds(value) -> int:
        """'HH:MM:SS'（Time 序列化格式，config_base.py:66-69）→ 秒；非法回退 300。"""
        try:
            parts = str(value).split(':')
            h, m, s = (int(parts[0]), int(parts[1]), int(parts[2])) if len(parts) == 3 else (0, 0, 300)
            return max(0, h * 3600 + m * 60 + s)
        except Exception:
            return 300

    @staticmethod
    def _compute_schedule(raw: dict):
        """遍历任务组取最早 next_run 与 pending 数（只读 json，不实例化 ConfigModel）。

        next_run 持久化在各任务 scheduler.next_run（str，strptime 先例 config.py:143/163）。
        anti_ban 会在内存里把 next_run 往后推（script.py:334-336），json 快照读不到该
        内存值——读到的时间偏早，域调度会提前把它当目标，方向安全（宁早勿晚）。
        """
        now = datetime.now()
        earliest = None
        pending = 0
        for key, task in raw.items():
            if key in _NON_TASK_KEYS or not isinstance(task, dict):
                continue
            scheduler = task.get('scheduler')
            if not isinstance(scheduler, dict):
                continue
            if not scheduler.get('enable', False):
                continue
            value = scheduler.get('next_run')
            if not value or not isinstance(value, str):
                continue
            try:
                next_run = datetime.strptime(value, NEXT_RUN_FORMAT)
            except ValueError:
                continue
            if earliest is None or next_run < earliest:
                earliest = next_run
            if next_run <= now:
                pending += 1
        return earliest, pending

    def __str__(self):
        return (f'InstanceInfo({self.name}, domain={self.domain}, type={self.resolved_type}, '
                f'next_run={self.next_run}, pending={self.pending_count}, task={self.running_task!r})')


class InstanceScheduler:
    """实例调度域的常驻元调度器：聚合 → 判态 → 决策 → 执行（关先开后）→ 自愈收敛。"""

    def __init__(self, poll_interval: int = DEFAULT_POLL_INTERVAL, server_port: int = 22270,
                 domain: str = 'default', config_dir: str = None):
        # config 目录相对 cwd（server/实例进程均从 repo 根启动，ConfigManager 同款约定）
        self.config_dir = Path.cwd() / 'config' if config_dir is None else Path(config_dir)
        self.poll_interval = max(MIN_POLL_INTERVAL, int(poll_interval))
        self.server_port = server_port
        self.domain = domain
        # 域内成员缓存（name → InstanceInfo），refresh() 重建
        self.members = {}
        # 防抖：上次「切换动作」（关或开）完成时刻
        self.last_switch_time = None
        # 自愈连败计数（连续 N 轮开目标失败 → 推送通知）
        self.heal_fail_count = 0
        # 已推送过通知的标记（避免每轮重复推送）
        self.heal_notified = False
        # 调用方显式注入的进程探针（name → probe），覆盖自动探测
        self._custom_probes = {}
        # queue_mode 双主冲突已警告集合（每域名只警告一次）
        self._queue_mode_warned = set()

    # ------------------------- 第 1 轮：域成员聚合与 next_run -------------------------

    def _list_config_files(self) -> list:
        """枚举 config/*.json 排除 template（仿 ConfigManager.all_script_files，
        module/server/config_manager.py:12-30）。"""
        if not self.config_dir.is_dir():
            return []
        result = []
        for p in sorted(self.config_dir.glob('*.json')):
            if p.stem == 'template':
                continue
            result.append(p)
        return result

    def refresh(self) -> list:
        """重建域成员快照：只读 json 聚合 enable/domain，返回本域成员列表。"""
        members = {}
        for path in self._list_config_files():
            try:
                raw = read_file(str(path))
            except Exception as e:
                logger.warning(f'[instance_schedule] read config failed: {path}: {e}')
                continue
            if not isinstance(raw, dict) or not raw:
                continue
            try:
                info = InstanceInfo(name=path.stem, path=str(path), raw=raw)
            except Exception as e:
                logger.warning(f'[instance_schedule] parse config failed: {path}: {e}')
                continue
            if not info.enable:
                continue  # 未启用实例调度：不纳入域（行为与现状一致）
            members[info.name] = info
        self.members = {n: i for n, i in members.items() if i.domain == self.domain}
        self._warn_queue_mode_conflict()
        if self.members:
            logger.info(f'[instance_schedule] domain {self.domain!r} members: '
                        f'{sorted(self.members)}')
        return list(self.members.values())

    def _warn_queue_mode_conflict(self):
        """双主冲突警告：域内多实例且任一成员开了 queue_mode（自动唤醒与元调度器抢主）。"""
        if len(self.members) < 2:
            return
        conflicted = [n for n, i in self.members.items()
                      if bool(i.raw.get('script', {}).get('optimization', {}).get('queue_mode', False))]
        if not conflicted:
            return
        key = (self.domain, tuple(sorted(conflicted)))
        if key in self._queue_mode_warned:
            return
        self._queue_mode_warned.add(key)
        logger.warning(f'[instance_schedule] domain {self.domain!r} 成员 {conflicted} 启用了 '
                       f'queue_mode（排队互斥）——与实例调度域互斥语义冲突（双主）：排队模式会在 '
                       f'空闲期自行开关模拟器，可能抵消或对抗元调度器的开关动作。建议域内成员 '
                       f'queue_mode=False（config/template.json script.optimization.queue_mode）')

    # ------------------------- 第 2 轮：进程探针组装与三态判据 -------------------------

    def _probe_for(self, info: InstanceInfo):
        """为成员组装进程探针：托管 ProcessProbe > pid 文件 PidFileProbe > HostProbe。

        优先级：调用方显式注入的探针（register_probe）> pid 文件约定
        log/<config_name>.pid > web server 托管探测（server_port 配置时）。
        全都没有时返回 None（未知态，由设备级探测兜底）。
        """
        if info.name in self._custom_probes:
            return self._custom_probes[info.name]
        pid_file = Path.cwd() / 'log' / f'{info.name}.pid'
        if pid_file.is_file():
            return PidFileProbe(pid_file)
        if self.server_port:
            return HostProbe(info.name, port=self.server_port)
        return None

    def register_probe(self, name: str, probe):
        """调用方（如 GUI/server 内嵌元调度器）显式注入探针；覆盖自动探测。"""
        self._custom_probes[name] = probe

    def probe_process_alive(self, info: InstanceInfo):
        """实例进程存活三值：True/False/None（未知）。None 不参与死亡判定。"""
        probe = self._probe_for(info)
        if probe is None:
            return None
        try:
            return probe.is_alive()
        except Exception as e:
            logger.warning(f'[instance_schedule] probe {info.name} failed: {e}')
            return None

    def state_of(self, info: InstanceInfo) -> InstanceState:
        """三态判据（T10 v2）：

        - 死亡：进程探测明确为 False（探测 None 不算死——设备级探测兜底，宁稳勿错杀）
        - 空闲：存活且 running_task 为空且无 pending 任务
        - 运行中：存活且（running_task 非空 或 有 pending）——不动作
        """
        alive = self.probe_process_alive(info)
        if alive is False:
            return InstanceState.DEAD
        # alive 为 True 或 None（未知）时按自报数据分空闲/运行中
        if info.running_task or info.pending_count > 0:
            return InstanceState.RUNNING
        return InstanceState.IDLE

    def suspect_scheduler_dead(self, info: InstanceInfo) -> bool:
        """疑似调度死（R1 定稿）：自报 running_task 非空，但进程探测明确死亡。

        处置：先 restart 实例进程（托管形态），失败或 CLI 形态才走错误切换。
        """
        return info.running_task and self.probe_process_alive(info) is False

    # ------------------------- 第 3 轮：设备开关层（游戏进程级） -------------------------

    _pw = None  # PlatformWindows 惰性单例（无参构造安全，已实测）

    def _platform(self):
        """惰性构造 PlatformWindows（Windows-only；导入失败时返回 None 并记一次日志）。"""
        if InstanceScheduler._pw is None:
            try:
                from module.device.platform2.platform_windows import PlatformWindows
                InstanceScheduler._pw = PlatformWindows()
            except Exception as e:
                logger.error(f'[instance_schedule] init PlatformWindows failed: {e}')
                InstanceScheduler._pw = False
        return InstanceScheduler._pw or None

    def _resolve_emulator_instance(self, info: InstanceInfo):
        """把 config 快照解析成 EmulatorInstance（platform2 现役 dataclass，
        emulator_windows.py:62）：优先全机枚举按 serial/name 匹配，找不到按 config 手搓。"""
        pw = self._platform()
        if pw is None:
            return None
        from module.device.platform2.emulator_base import get_serial_pair
        serial = info.serial
        port_serial, _ = get_serial_pair(serial)
        if port_serial is not None:
            serial = port_serial  # emulator-5554+X 归一化为 127.0.0.1:port（platform_base.py:116-120 先例）
        try:
            for inst in pw.all_emulator_instances:  # 全机实例枚举（emulator_windows.py:577-587）
                if inst.serial == serial or (info.emulatorinfo_name and inst.name == info.emulatorinfo_name):
                    return inst
        except Exception as e:
            logger.warning(f'[instance_schedule] enumerate instances failed: {e}')
        # 枚举不中：用 config 的 emulatorinfo 手搓（dataclass 直构），path 缺失则无法操作
        if not info.emulatorinfo_path:
            logger.error(f'[instance_schedule] {info.name}: 无法定位模拟器实例'
                         f'（serial={info.serial} 无枚举匹配且 emulatorinfo_path 为空）')
            return None
        from module.device.platform2.emulator_windows import EmulatorInstance
        return EmulatorInstance(serial=serial, name=info.emulatorinfo_name,
                                path=info.emulatorinfo_path)

    def _emulator_running(self, info: InstanceInfo) -> bool:
        """模拟器实例设备是否开着（偏保守：宁可报开着，漏关比重复关代价大）。

        psutil 扫描模拟器 exe 进程（EmulatorManager.iter_running_emulator，
        emulator_windows.py:499-524），exe 与实例 emulatorinfo_path 同目录即视为
        该模拟器有进程在跑；同安装根多开时无法按进程区分实例（局限，推断），
        但关闭动作本身走实例定向 CLI，对已关实例幂等无害。
        """
        pw = self._platform()
        if pw is None or not info.emulatorinfo_path:
            return False
        try:
            install_dir = os.path.dirname(os.path.abspath(info.emulatorinfo_path)).lower()
            for exe in pw.iter_running_emulator():
                if os.path.dirname(os.path.abspath(exe)).lower() == install_dir:
                    return True
        except Exception as e:
            logger.warning(f'[instance_schedule] emulator running check failed: {e}')
        return False

    def _desktop_running(self, info: InstanceInfo) -> bool:
        """桌面实例设备是否开着：档案状态文件 PID + desktop_pid_alive（desktop_impl.py:479-489/601）。"""
        if not info.desktop_profile_dir:
            return False
        try:
            from module.device.method.desktop_impl import (desktop_pid_alive,
                                                           desktop_read_instance_state)
            state = desktop_read_instance_state(Path(info.desktop_profile_dir))
            pid = state.get('pid')
            return bool(pid and desktop_pid_alive(pid))
        except Exception as e:
            logger.warning(f'[instance_schedule] desktop running check failed: {e}')
            return False

    def device_running(self, info: InstanceInfo) -> bool:
        """设备级运行探测（三形态分发）。云手机恒 True（云端常开，T9 结论）。"""
        if info.resolved_type == 'desktop':
            return self._desktop_running(info)
        if info.resolved_type == 'cloud_phone':
            return True
        return self._emulator_running(info)

    @staticmethod
    def _force_kill_pid(pid: int, wait: float = 10.0) -> bool:
        """强杀 PID 并轮询确认退出（仿 desktop_impl._desktop_kill_pids 的 kernel32 模式，
        desktop_impl.py:1188-1226；等待间隔 ≥3 合规）。"""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        from module.device.method.desktop_impl import desktop_pid_alive
        kernel32 = ctypes.windll.kernel32
        deadline = time.time() + wait
        while True:
            if not desktop_pid_alive(pid):
                return True
            handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if handle:
                try:
                    kernel32.TerminateProcess(handle, 0)
                    logger.info(f'[instance_schedule] force kill pid={pid}')
                finally:
                    kernel32.CloseHandle(handle)
            if time.time() >= deadline:
                return not desktop_pid_alive(pid)
            time.sleep(3)

    def _close_emulator(self, info: InstanceInfo) -> bool:
        """关模拟器实例：直调 _emulator_stop（绕过 _emulator_function_wrapper 写死
        self.emulator_instance 的 platform_windows.py:280），失败仅重试直调。

        红线：绝不改调公有 emulator_stop——它失败重试时会先 start 再 stop
        （platform_windows.py:460-472 反拉坑），会把已关实例重新拉起。
        _emulator_stop 内部按品牌发 CLI/psutil（L205-269），不依赖 ADB 连接。
        """
        pw = self._platform()
        instance = self._resolve_emulator_instance(info)
        if pw is None or instance is None:
            return False
        for attempt in range(1, 4):
            try:
                pw._emulator_stop(instance)
                logger.info(f'[instance_schedule] emulator stop issued: {info.name} ({instance})')
                # stop 是异步 CLI/kill，给一拍缓冲再探
                time.sleep(3)
                if not self._emulator_running(info):
                    return True
                logger.warning(f'[instance_schedule] {info.name} still running after stop '
                               f'(attempt {attempt}/3)')
            except Exception as e:
                logger.warning(f'[instance_schedule] emulator stop failed: {info.name}: {e} '
                               f'(attempt {attempt}/3)')
                time.sleep(3)
        logger.error(f'[instance_schedule] emulator stop failed 3 times: {info.name}')
        return False

    def _close_desktop(self, info: InstanceInfo) -> bool:
        """关桌面实例：读档案状态文件 oas_instance.json 的 PID 强杀
        （desktop_impl.py:468-514 状态文件 + desktop_pid_alive:601）。

        局限（推断）：启动器进程（onmyoji.exe 派生者）PID 只在 worker 进程内存
        （_desktop_spawned_pids），元调度器跨进程拿不到，可能残留启动器进程——
        它无窗口、不干扰下一次窗口识别，代价可接受。
        """
        try:
            from module.device.method.desktop_impl import (desktop_clear_instance_state,
                                                           desktop_pid_alive,
                                                           desktop_read_instance_state)
            profile = Path(info.desktop_profile_dir)
            state = desktop_read_instance_state(profile)
            pid = state.get('pid')
            if not pid or not desktop_pid_alive(pid):
                logger.info(f'[instance_schedule] desktop not running: {info.name}')
                desktop_clear_instance_state(profile)
                return True
            ok = self._force_kill_pid(pid)
            desktop_clear_instance_state(profile)
            logger.info(f'[instance_schedule] desktop stop pid={pid}: {info.name} -> {ok}')
            return ok
        except Exception as e:
            logger.error(f'[instance_schedule] desktop stop failed: {info.name}: {e}')
            return False

    def _cloud_adb(self, info: InstanceInfo):
        """云手机 ADB 直连（adbutils，仓库既有依赖）：connect 远程 serial。失败返回 None。"""
        if info.serial in ('auto', ''):
            return None
        try:
            import adbutils
            client = adbutils.AdbClient(host='127.0.0.1', port=5037)
            try:
                client.connect(info.serial, timeout=5)
            except Exception:
                pass  # 已连接时会抛/或静默，统一靠 AdbDevice.shell 暴露真实错误
            return adbutils.AdbDevice(client, info.serial)
        except Exception as e:
            logger.warning(f'[instance_schedule] cloud adb connect failed: {info.name}: {e}')
            return None

    def _close_cloud_phone(self, info: InstanceInfo) -> bool:
        """关云手机实例：降级为游戏进程级 am force-stop（adb.py:311-315 语义）。

        云手机无设备级开关 API（T9 结论），设备常开；package_name=auto 时无法
        确定游戏包名，跳过并记录（宁少动勿误杀）。
        """
        if info.package_name in ('auto', ''):
            logger.info(f'[instance_schedule] cloud phone {info.name}: package_name=auto, '
                        f'skip game-level stop')
            return True
        device = self._cloud_adb(info)
        if device is None:
            return False
        try:
            device.shell(['am', 'force-stop', info.package_name])
            logger.info(f'[instance_schedule] cloud phone game stopped: {info.name} '
                        f'({info.package_name})')
            return True
        except Exception as e:
            logger.warning(f'[instance_schedule] cloud phone stop failed: {info.name}: {e}')
            return False

    def close_instance(self, info: InstanceInfo) -> bool:
        """关实例设备（游戏进程级，三形态分发）。"""
        if info.resolved_type == 'desktop':
            return self._close_desktop(info)
        if info.resolved_type == 'cloud_phone':
            return self._close_cloud_phone(info)
        return self._close_emulator(info)

    def _open_emulator(self, info: InstanceInfo) -> bool:
        """开模拟器实例：直调 _emulator_start 按品牌发 CLI（platform_windows.py:167-203）。

        不调 emulator_start_watch（它依赖 ConnectionAttr 的 adb_client/serial，无参
        PlatformWindows 上不可用）——启动完成确认由实例进程 Device.__init__/
        Restart 分支自行完成（script.py:427-431 已禁用域成员空闲预热，到点走 Restart）。
        """
        pw = self._platform()
        instance = self._resolve_emulator_instance(info)
        if pw is None or instance is None:
            return False
        try:
            pw._emulator_start(instance)
            logger.info(f'[instance_schedule] emulator start issued: {info.name} ({instance})')
            return True
        except Exception as e:
            logger.error(f'[instance_schedule] emulator start failed: {info.name}: {e}')
            return False

    def _open_desktop(self, info: InstanceInfo) -> bool:
        """开桌面实例：desktop_launch_instance（desktop_impl.py:713-759，模块级函数）。

        用 SimpleNamespace 伪装 config（该函数只读 config.script.desktop 四字段：
        enable/game_path/profile_dir/window_title），不实例化 ConfigModel。
        已在运行（状态文件 PID 活）时它内部幂等跳过（L740-744）。
        """
        if not info.desktop_game_path and not info.desktop_profile_dir:
            logger.error(f'[instance_schedule] desktop {info.name}: game_path 与 profile_dir '
                         f'均为空，无法定位安装目录')
            return False
        fake_config = SimpleNamespace(script=SimpleNamespace(desktop=SimpleNamespace(
            enable=True, game_path=info.desktop_game_path,
            profile_dir=info.desktop_profile_dir, window_title=info.desktop_window_title,
        )))
        try:
            from module.device.method.desktop_impl import desktop_launch_instance
            return bool(desktop_launch_instance(fake_config))
        except Exception as e:
            logger.error(f'[instance_schedule] desktop launch failed: {info.name}: {e}')
            return False

    def _open_cloud_phone(self, info: InstanceInfo) -> bool:
        """开云手机实例：降级为游戏进程级 monkey 启动（adb.py:256-270 语义）。

        设备云端常开无需开；package_name=auto 时跳过（无法确定游戏包）。
        """
        if info.package_name in ('auto', ''):
            logger.info(f'[instance_schedule] cloud phone {info.name}: package_name=auto, '
                        f'skip game-level start（设备常开，游戏由实例进程 app_start 拉起）')
            return True
        device = self._cloud_adb(info)
        if device is None:
            return False
        try:
            device.shell(['monkey', '-p', info.package_name, '-c',
                          'android.intent.category.LAUNCHER', '--pct-syskeys', '0', '1'])
            logger.info(f'[instance_schedule] cloud phone game started: {info.name}')
            return True
        except Exception as e:
            logger.warning(f'[instance_schedule] cloud phone start failed: {info.name}: {e}')
            return False

    def open_instance(self, info: InstanceInfo) -> bool:
        """开实例设备（三形态分发）。幂等：已开着直接返回 True。"""
        if info.resolved_type != 'cloud_phone' and self.device_running(info):
            logger.info(f'[instance_schedule] device already running, skip open: {info.name}')
            return True
        if info.resolved_type == 'desktop':
            return self._open_desktop(info)
        if info.resolved_type == 'cloud_phone':
            return self._open_cloud_phone(info)
        return self._open_emulator(info)

    # ------------------------- 第 4 轮：切换决策、自愈与收敛 -------------------------

    def _pick_target(self) -> InstanceInfo:
        """域内 next_run 最早的成员为目标（None 视为无穷远，排最后）。"""
        members = list(self.members.values())
        if not members:
            return None
        return min(members, key=lambda i: (i.next_run is None,
                                           i.next_run or datetime.max))

    def _notify(self, title: str, content: str):
        """推送通知：用目标成员 config 的 notify_config/notify_enable 直构 Notifier
        （module/notify/notify.py:18-24 只需这两个值），不实例化 ConfigModel。"""
        logger.critical(f'[instance_schedule] {title}: {content}')
        for info in self.members.values():
            try:
                from module.notify.notify import Notifier
                Notifier(info.notify_config, enable=info.notify_enable).push(
                    title=title, content=f'<{info.name}> {content}')
                return  # 一份配置推一次即可，避免重复轰炸
            except Exception as e:
                logger.warning(f'[instance_schedule] notify failed: {e}')

    def _switch_allowed(self, target: InstanceInfo) -> bool:
        """切换防抖（T15 定稿语义）：两次切换之间的最小间隔。"""
        if self.last_switch_time is None:
            return True
        remain = target.switch_threshold - (time.time() - self.last_switch_time)
        if remain > 0:
            logger.info(f'[instance_schedule] switch throttled, remain {remain:.0f}s '
                        f'(switch_threshold={target.switch_threshold}s)')
            return False
        return True

    def _close_others(self, target: InstanceInfo) -> None:
        """先关其他（用户指定顺序）：非目标 ∧ 设备开 ∧ 非运行中 → 关。

        非目标且 running_task 非空（自报在跑任务）→ 顺延不硬切，下轮收敛——
        硬切会打断正在跑的任务，交给收敛逻辑在它空闲后处理。
        """
        if not target.close_others_on_switch:
            logger.info(f'[instance_schedule] close_others_on_switch=False, skip closing others')
            return
        for other in self.members.values():
            if other.name == target.name:
                continue
            if not self.device_running(other):
                continue  # 幂等：设备未开，跳过
            if other.running_task:
                logger.info(f'[instance_schedule] {other.name} is running task '
                            f'{other.running_task!r}, defer closing (converge next round)')
                continue
            logger.info(f'[instance_schedule] closing other instance: {other.name}')
            self.close_instance(other)

    def poll_once(self) -> None:
        """单轮决策：聚合 → 疑似调度死处理 → 目标切换 → 自愈 → 违规多开收敛。"""
        self.refresh()
        if not self.members:
            return

        # R1：疑似调度死（自报在跑但进程死了）→ 先 restart 实例进程，不直接错误切换
        for info in self.members.values():
            if self.suspect_scheduler_dead(info):
                logger.warning(f'[instance_schedule] suspect scheduler dead: {info.name} '
                               f'(running_task={info.running_task!r}, process dead)')
                probe = self._probe_for(info)
                if probe is not None and probe.restart():
                    logger.info(f'[instance_schedule] restarted scheduler process: {info.name}')
                    continue
                # restart 不可用（CLI 形态）→ 按错误切换处置
                logger.warning(f'[instance_schedule] restart unavailable, fallback to '
                               f'on-error close: {info.name} (on_error={info.on_error})')
                if info.on_error in ('close_game', 'close_and_switch'):
                    self.close_instance(info)

        target = self._pick_target()
        if target is None:
            return

        # 目标设备未开 → 切换：先关其他，再开目标
        if not self.device_running(target):
            if not self._switch_allowed(target):
                return
            logger.info(f'[instance_schedule] switching to {target.name} '
                        f'(next_run={target.next_run})')
            self._close_others(target)
            if self.open_instance(target):
                self.last_switch_time = time.time()
                self.heal_fail_count = 0
                self.heal_notified = False
            else:
                self.heal_fail_count += 1
                logger.error(f'[instance_schedule] open target failed: {target.name} '
                             f'(fails={self.heal_fail_count}/3)')
                if self.heal_fail_count >= 3 and not self.heal_notified:
                    self._notify('实例调度：目标开启失败',
                                 f'{target.name} 连续 3 轮开启失败，请人工检查')
                    self.heal_notified = True
            return

        # 目标设备已开：若目标进程死了且没有别的实例活着 → 自愈兜底
        alive = self.probe_process_alive(target)
        if alive is False:
            logger.warning(f'[instance_schedule] target {target.name} device running but '
                           f'scheduler process dead, self-heal: restart or reopen')
            probe = self._probe_for(target)
            if probe is not None and probe.restart():
                return
            # 托管重启不可用：设备还开着，等实例进程由部署方拉起；不做设备级动作
            return

        # 违规多开收敛：域内除目标外还有设备开着的（含上轮顺延的），在目标运行中
        # 时不动作（等它跑完），空闲成员则逐个关闭——最终收敛到「只开目标」
        if target.close_others_on_switch:
            for other in self.members.values():
                if other.name == target.name:
                    continue
                if self.device_running(other) and not other.running_task:
                    logger.info(f'[instance_schedule] converge: closing stray instance '
                                f'{other.name} (target {target.name} holds the domain)')
                    self.close_instance(other)

