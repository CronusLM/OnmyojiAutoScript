# This Python file uses the following encoding: utf-8
# 实例调度（元调度器）模块
# 设计依据：T10 方案 v3 + 用户 6 条澄清（见任务板 task-10/task-14）
#
# 职责边界：
#   - 决策数据 = 各 config/*.json 只读快照（不实例化 ConfigModel，避免 pydantic 校验开销
#     与 ConfigModel.__setattr__ 的自动落盘风险，见 module/config/config_model.py:170-179）
#   - 实例设备开关 = 直调 module/device/platform2 的 CLI/psutil 能力（不构造 Device、
#     不依赖本进程 ADB 连接；EmulatorManager.emulator_start(serial,...) 旧类为死代码，
#     现役 API 为 PlatformWindows._emulator_start/_emulator_stop，见 platform_windows.py:167-269）
#   - 跨实例互斥 = 复用 module/config/instance_guard.py 的共享状态文件思路，
#     元调度器自身不写 queue_state.json（queue_mode 归实例进程自治，域内启用元调度器时
#     建议 queue_mode=False，避免双主，见 T13 记录）
#
# 接口冻结（T15 一致）：script.instance_schedule 6 字段
#   enable / domain / instance_type / on_error / close_others_on_switch / switch_threshold

from module.instance_schedule.scheduler import (
    InstanceScheduler,
    InstanceInfo,
    InstanceState,
    ProcessProbe,
    PidFileProbe,
    HostProbe,
    MIN_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
)

__all__ = [
    'InstanceScheduler',
    'InstanceInfo',
    'InstanceState',
    'ProcessProbe',
    'PidFileProbe',
    'HostProbe',
    'MIN_POLL_INTERVAL',
    'DEFAULT_POLL_INTERVAL',
]
