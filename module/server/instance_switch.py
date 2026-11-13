# This Python file uses the following encoding: utf-8
# @author kronus
# 配置实例轮换管理器
# github https://github.com/CronusLM/OnmyojiAutoScript
import asyncio
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from asyncio import sleep

from module.logger import logger
from module.server.script_process import ScriptProcess, ScriptState


class InstanceSwitchManager:
    """
    配置实例轮换管理器

    同一时刻只运行一个配置实例。当运行中的配置
    1) 遇到错误停止（状态 WARNING / 子进程异常退出），或
    2) 进入空闲（任务队列为空，script.py 经 state_queue 上报 {"idle": True}）
    时，停止当前配置并切换到下一个参与轮换的配置
    （script.switcher.enable 开启的配置）；另一个配置遇到错误或空闲时再切换回来。

    - 空闲触发的切换受冷却时间限制（默认 10 分钟，取发起配置的设置），错误触发不受限制
    - 手动启动 = 该配置立即成为活动配置（旧活动配置先停止）
    - 手动停止 = 轮换暂停，直到下一次空闲事件
    - 服务重启后自动恢复轮换（从上次活动配置继续，持久化为暂停状态时除外）

    状态持久化到 log/.switch_state.json（先例：module/instance_guard.py 的 .queue_state.json）。
    本管理器的轮询循环运行在推送线程的事件循环中（见 MainManager.push_data_handle），
    与 uvicorn 主循环通过简单标志共享状态，与现有 mm.script_process 的共享方式一致；
    切换动作用非阻塞锁防止并发重入。
    """

    STATE_FILE = Path.cwd() / 'log' / '.switch_state.json'
    HISTORY_LIMIT = 50  # 切换历史最多保留条数
    POLL_INTERVAL = 3  # 轮询间隔（秒），云手机规则：不低于 3 秒
    DEFAULT_COOLDOWN_MINUTES = 10  # 读取配置失败时的默认空闲冷却

    def __init__(self) -> None:
        self._mm = None  # 惰性解析，避免与 main_manager 的循环导入
        self.active: Optional[str] = None  # 当前活动配置
        self.paused: bool = False  # 手动停止后暂停轮换，等待下一次空闲事件恢复
        self.idle_flags: set[str] = set()  # 已上报空闲且尚未恢复的配置集合
        self.last_switch_time: float = 0.0  # 上次切换完成的时间戳（空闲冷却用）
        self.history: list[dict] = []  # 切换历史 {time, from, to, reason}
        self._rotate_cursor: dict[str, str] = {}  # 每个发起配置上次切换到的目标，用于多配置轮转
        self._switch_lock = threading.Lock()  # 防止并发切换重入（非阻塞获取）
        # 配置读取缓存（Config(name) 每次构造都重读 JSON + Pydantic 校验，轮询频率下太重）
        self._participants_cache: tuple[float, list[str]] = (0.0, [])  # (到期时间戳, 参与配置列表)
        self._load_state()

    @property
    def mm(self):
        """MainManager 单例，首次访问时再导入，规避模块循环导入"""
        if self._mm is None:
            from module.server.main_manager import mm
            self._mm = mm
        return self._mm

    # ----------------------------- 持久化 -----------------------------
    def _load_state(self) -> None:
        """启动时读取上次轮换状态（恢复动作在 resume_on_startup 中执行）"""
        try:
            if not self.STATE_FILE.exists():
                return
            data = json.loads(self.STATE_FILE.read_text(encoding='utf-8'))
            self.active = data.get('active')
            self.paused = bool(data.get('paused', False))
            self.history = data.get('history', [])[-self.HISTORY_LIMIT:]
            logger.info(f'[Switcher] state loaded: active={self.active}, paused={self.paused}')
        except Exception as e:
            logger.warning(f'[Switcher] load state failed: {e}')

    def _save_state(self) -> None:
        try:
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'active': self.active,
                'paused': self.paused,
                'history': self.history[-self.HISTORY_LIMIT:],
            }
            self.STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            logger.warning(f'[Switcher] save state failed: {e}')

    def _add_history(self, from_config: Optional[str], to_config: Optional[str], reason: str) -> None:
        self.history.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'from': from_config,
            'to': to_config,
            'reason': reason,
        })
        self.history = self.history[-self.HISTORY_LIMIT:]

    # ----------------------------- 参与配置 -----------------------------
    def _participants(self) -> list[str]:
        """参与轮换的配置：script.switcher.enable 开启的配置，按名称排序保证切换顺序稳定

        Config(name) 每次构造都会重读 JSON + Pydantic 校验，轮询频率下开销大，
        结果缓存 30 秒；GUI 改配置最多延迟一个缓存周期生效。
        """
        now = time.time()
        expires, cached = self._participants_cache
        if now < expires:
            return cached
        result = []
        for name in sorted(self.mm.script_process.keys()):
            try:
                config = self.mm.config_cache(name)
                if config.model.script.switcher.enable:
                    result.append(name)
            except Exception as e:
                logger.warning(f'[Switcher] read switcher config of {name} failed: {e}')
        self._participants_cache = (now + 30, result)
        return result

    def _idle_cooldown_seconds(self, config_name: Optional[str]) -> int:
        """空闲切换冷却（秒），取发起配置的设置，读取失败用默认值

        与 _participants 相同，只在切换时调用，频率低，不加缓存。
        """
        minutes = self.DEFAULT_COOLDOWN_MINUTES
        if config_name:
            try:
                config = self.mm.config_cache(config_name)
                minutes = max(0, int(config.model.script.switcher.idle_cooldown_minutes))
            except Exception:
                pass
        return minutes * 60

    # ----------------------------- 轮询循环 -----------------------------
    async def coroutine_loop(self) -> None:
        """轮询循环：检测活动配置的错误停止/异常退出，以及冷却到期后的待执行空闲切换"""
        logger.info('[Switcher] instance switcher loop started')
        try:
            while 1:
                await sleep(self.POLL_INTERVAL)
                await self._check_active()
        except asyncio.CancelledError:
            logger.warning('[Switcher] instance switcher loop cancelled')
            return
        except Exception as e:
            logger.error(f'[Switcher] loop exited unexpectedly: {e}')

    async def _check_active(self) -> None:
        if self.paused or not self.active:
            return
        sp = self.mm.script_process.get(self.active)
        if sp is None:
            # 配置被删除/重命名，视为手动停止，暂停轮换
            logger.warning(f'[Switcher] active config {self.active} disappeared, pause rotation')
            self.active = None
            self.paused = True
            self._save_state()
            return
        # 错误停止：子进程上报 WARNING
        # 活动配置未参与轮换（手动启动的非轮换配置），引擎不接管其错误/空闲
        if self.active not in self._participants():
            return
        if sp.state == ScriptState.WARNING:
            await self._trigger_switch(self.active, 'error')
            return
        # 子进程已退出
        if sp._process is not None and not sp._process.is_alive():
            exitcode = sp._process.exitcode
            if exitcode is None:
                return  # 尚未完全退出，下一轮再查
            if exitcode != 0:
                await self._trigger_switch(self.active, 'error')
            else:
                # 正常退出（未经 stop() 的外部终止），暂停轮换等待下一次空闲事件
                logger.warning(f'[Switcher] active config {self.active} exited with code 0, pause rotation')
                self.idle_flags.discard(self.active)
                self.active = None
                self.paused = True
                self._save_state()
            return
        # 空闲切换：空闲事件发生时若冷却未到会跳过，这里在冷却到期后补触发
        if self.active in self.idle_flags:
            elapsed = time.time() - self.last_switch_time
            cooldown = self._idle_cooldown_seconds(self.active)
            if elapsed >= cooldown:
                await self._trigger_switch(self.active, 'idle')

    # ----------------------------- 空闲事件 -----------------------------
    def submit_idle(self, config_name: str, idle: bool) -> None:
        """空闲事件入口，由 ScriptProcess 状态协程在收到 {"idle": ...} 时调用"""
        if idle:
            self.idle_flags.add(config_name)
        else:
            self.idle_flags.discard(config_name)
            return
        try:
            asyncio.ensure_future(self._on_idle(config_name))
        except Exception as e:
            logger.error(f'[Switcher] submit idle event of {config_name} failed: {e}')

    async def _on_idle(self, config_name: str) -> None:
        logger.info(f'[Switcher] config {config_name} reports idle')
        # 非参与配置不被引擎接管：仅清除空闲标记
        if config_name not in self._participants():
            self.idle_flags.discard(config_name)
            return
        if self.paused:
            logger.info('[Switcher] rotation resumes from paused state by idle event')
            self.paused = False
            self._save_state()
        if self.active and self.active != config_name:
            return
        if self.active != config_name:
            # 暂停期间无活动配置：空闲事件后恢复轮换，上报者成为活动配置（不立即切换）
            self.active = config_name
            self.last_switch_time = time.time()
            self._save_state()
            return
        elapsed = time.time() - self.last_switch_time
        cooldown = self._idle_cooldown_seconds(config_name)
        if elapsed < cooldown:
            remaining = cooldown - elapsed
            logger.info(f'[Switcher] idle switch cooldown, {remaining:.0f}s left')
            return
        await self._trigger_switch(config_name, 'idle')

    # ----------------------------- 切换 -----------------------------
    async def _trigger_switch(self, from_config: str, reason: str) -> None:
        """自动切换：选择下一个参与配置并执行；无其他参与配置时暂停轮换"""
        if not self._switch_lock.acquire(blocking=False):
            logger.info('[Switcher] switch already in progress, skip')
            return
        try:
            # 执行前复核：活动配置已变（如用户手动停止/切换），放弃本次自动切换
            if self.active != from_config:
                return
            participants = self._participants()
            targets = [name for name in participants if name != from_config]
            if not targets:
                # 只有当前配置参与轮换：保持它继续运行，仅暂停引擎（不停机）
                logger.warning('[Switcher] no other switcher-enabled config, pause rotation and keep current running')
                self.idle_flags.discard(from_config)
                self.paused = True
                self._add_history(from_config, None, f'{reason}_no_target')
                self._save_state()
                return
            # 多配置轮转：从上次目标之后继续选，避免固定切到同一个配置
            last_to = self._rotate_cursor.get(from_config)
            idx = targets.index(last_to) + 1 if last_to in targets else 0
            target = targets[idx % len(targets)]
            success = await self._do_switch(from_config, target, reason)
            if success:
                self._rotate_cursor[from_config] = target
        finally:
            self._switch_lock.release()

    async def _do_switch(self, from_config: Optional[str], to_config: str, reason: str) -> bool:
        """停止 from 配置并启动 to 配置，更新活动配置与历史（调用方需持有切换锁）"""
        logger.info(f'[Switcher] switch {from_config} -> {to_config} (reason: {reason})')
        if from_config:
            self.idle_flags.discard(from_config)
            sp = self.mm.script_process.get(from_config)
            if sp is not None and sp.state != ScriptState.INACTIVE:
                await sp.stop()
        self.idle_flags.discard(to_config)
        sp_to = self.mm.script_process.get(to_config)
        if sp_to is None:
            logger.error(f'[Switcher] target config {to_config} does not exist, stop rotation')
            self.active = None
            self.paused = True
            self._add_history(from_config, to_config, f'{reason}_failed')
            self._save_state()
            return False
        await sp_to.start()
        self.active = to_config
        self.last_switch_time = time.time()
        self._add_history(from_config, to_config, reason)
        self._save_state()
        return True

    # ----------------------------- 手动操作 -----------------------------
    async def api_start(self, config_name: str) -> bool:
        """手动启动：该配置立即成为活动配置，旧活动配置先停止"""
        if config_name not in self.mm.script_process:
            try:
                self.mm.script_process[config_name] = ScriptProcess(config_name)
            except FileNotFoundError:
                logger.error(f'[Switcher] start failed, config {config_name} not found')
                return False
        old = self.active
        if old and old != config_name:
            sp_old = self.mm.script_process.get(old)
            if sp_old is not None and sp_old.state != ScriptState.INACTIVE:
                logger.info(f'[Switcher] manual start {config_name}, stop active config {old} first')
                await sp_old.stop()
            self.idle_flags.discard(old)
        self.idle_flags.discard(config_name)
        await self.mm.script_process[config_name].start()
        self.active = config_name
        self.paused = False
        self.last_switch_time = time.time()
        self._add_history(old, config_name, 'manual_start')
        self._save_state()
        logger.info(f'[Switcher] manual start {config_name}, it becomes the active config')
        return True

    async def api_stop(self, config_name: str = '') -> bool:
        """手动停止：停止指定配置（缺省为当前活动配置），轮换暂停"""
        name = config_name or self.active
        if not name:
            logger.warning('[Switcher] stop failed, no config specified and no active config')
            return False
        sp = self.mm.script_process.get(name)
        if sp is None:
            logger.warning(f'[Switcher] stop failed, config {name} does not exist')
            return False
        if sp.state != ScriptState.INACTIVE:
            await sp.stop()
        self.idle_flags.discard(name)
        if self.active == name:
            self.active = None
            self.paused = True
            self._add_history(name, None, 'manual_stop')
            logger.info(f'[Switcher] manual stop {name}, rotation paused until next idle event')
        else:
            logger.info(f'[Switcher] manual stop {name} (not active config)')
        self._save_state()
        return True

    async def api_restart(self, config_name: str = '') -> bool:
        """手动重启：重启指定配置（缺省为当前活动配置），重启后仍为活动配置"""
        name = config_name or self.active
        if not name or name not in self.mm.script_process:
            logger.warning(f'[Switcher] restart failed, config {name} does not exist')
            return False
        sp = self.mm.script_process[name]
        if sp.state != ScriptState.INACTIVE:
            await sp.stop()
        self.idle_flags.discard(name)
        await sp.start()
        self.active = name
        self.paused = False
        self.last_switch_time = time.time()
        self._add_history(name, name, 'manual_restart')
        self._save_state()
        logger.info(f'[Switcher] manual restart {name}')
        return True

    async def api_switch_to(self, to_config: str) -> bool:
        """手动切换：停止当前活动配置并启动目标配置（不受冷却限制）"""
        if to_config not in self.mm.script_process:
            try:
                self.mm.script_process[to_config] = ScriptProcess(to_config)
            except FileNotFoundError:
                logger.error(f'[Switcher] switch failed, config {to_config} not found')
                return False
        if not self._switch_lock.acquire(blocking=False):
            logger.info('[Switcher] switch already in progress, try later')
            return False
        try:
            old = self.active
            self.paused = False
            await self._do_switch(old if old != to_config else None, to_config, 'manual_switch')
            return True
        finally:
            self._switch_lock.release()

    # ----------------------------- 通知与恢复 -----------------------------
    def notify_config_removed(self, config_name: str) -> None:
        """配置被重命名/删除时由路由层通知；若为活动配置则暂停轮换"""
        self.idle_flags.discard(config_name)
        if self.active == config_name:
            logger.warning(f'[Switcher] active config {config_name} removed, pause rotation')
            self.active = None
            self.paused = True
            self._save_state()

    async def resume_on_startup(self) -> None:
        """服务启动后恢复轮换：从上次活动配置继续（持久化为暂停状态则不恢复）

        无任何持久化记录（功能上线后首次启动 / 状态文件被删）时自动引导：
        启动第一个参与轮换的配置作为活动配置，否则启动后零配置运行。
        """
        if self.paused:
            logger.info('[Switcher] rotation was paused, skip resume on startup')
            return
        if not self.active:
            # 首次启动引导：没有历史活动配置，从参与轮换的配置中选第一个启动
            participants = self._participants()
            if not participants:
                logger.info('[Switcher] no switcher-enabled config, skip resume on startup')
                return
            self.active = participants[0]
            logger.info(f'[Switcher] no last active config, bootstrap rotation with {self.active}')
        name = self.active
        if name not in self.mm.script_process:
            logger.warning(f'[Switcher] last active config {name} not found, skip resume')
            self.active = None
            self._save_state()
            return
        sp = self.mm.script_process[name]
        if sp.state == ScriptState.INACTIVE:
            logger.info(f'[Switcher] resume rotation with last active config {name}')
            await sp.start()
        else:
            logger.info(f'[Switcher] last active config {name} already running')
        self._add_history(None, name, 'startup_resume')
        self._save_state()

    def status(self) -> dict:
        """当前轮换状态（供 /switch/status 接口）"""
        return {
            'active': self.active,
            'paused': self.paused,
            'participants': self._participants(),
            'idle_configs': sorted(self.idle_flags),
            'last_switch_time': datetime.fromtimestamp(self.last_switch_time).strftime('%Y-%m-%d %H:%M:%S') if self.last_switch_time else '',
            'history': self.history,
        }


switcher = InstanceSwitchManager()  # 全局单例
