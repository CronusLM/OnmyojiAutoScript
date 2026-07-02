# OAS 仓库指南

## 项目概述

OAS（OnmyojiAutoScript）—— 阴阳师自动化脚本，Python 3.10+，Windows 平台。基于 AzurLaneAutoScript (Alas) 框架。

## 入口点

| 命令 | 用途 |
|------|------|
| `python gui.py` | 桌面 GUI（Fluent UI） |
| `python server.py` | Web 服务（FastAPI/uvicorn，默认 `0.0.0.0:22270`） |
| `python script.py` | 调度器主循环（`__main__` 中启动） |
| `docker compose up` | Docker 开发环境（`deploy/docker/Dockerfile`） |

## 目录结构

```
tasks/             每个游戏功能 = 一个子目录
  <Task>/          如 Orochi, RealmRaid, Delegation
    script_task.py   主线逻辑，结尾必须 raise TaskEnd
    config.py        Pydantic 配置模型
    assets.py        自动生成（勿手动编辑，由 dev_tools/assets_extract.py 生成）
    res/             模板图片 + image.json（数据源）
  Component/       通用行为组件（Mixin 多继承）
    GeneralBattle/   战斗循环、胜负检测、奖励处理
    GeneralRoom/     创建/退出房间
    GeneralInvite/   邀请、等待队友
    GeneralBuff/     加成面板
    SwitchSoul/      御魂预设切换
  GameUi/           页面导航引擎 + 页面有向图
module/            核心基础设施（设备连接、OCR、配置、异常、原子操作）
module/atom/       原子操作：RuleImage, RuleClick, RuleOcr, RuleSwipe, RuleList 等
module/device/     设备抽象：ADB + uiautomator2 + minitouch
deploy/            安装/部署/配置管理
dev_tools/         代码生成工具（assets_extract, generate_requirements）
fluentui/          C++ Fluent UI 组件（CMake 构建）
config/template.json  全局配置模板，每任务一项
```

## 任务框架约定

每个任务严格遵循三步模式：

1. **`config.py`** — 定义任务 Pydantic 配置模型，嵌套字段匹配 `template.json` 路径
2. **`res/`** — 截图 + 编辑 `image.json`，运行 `assets_extract.py` 生成 `assets.py`
3. **`script_task.py`** — `ScriptTask` 类继承 `GameUi` + 所需 Component Mixins + 自家 Assets

```
class ScriptTask(GeneralBattle, GeneralInvite, ..., GameUi, MyAssets):
    def run(self):
        # 1. 场景切换（ui_goto）
        # 2. 限次/限时检查 + 配置读取
        # 3. 主循环（while 1: screenshot + appear_then_click）
        # 4. set_next_run(...)
        raise TaskEnd
```

所有任务 `config.py` 统一在 `module/config/config_model.py` 中被导入注册。

## 资产系统

**数据源 → 生成目标**：`res/image.json` → `dev_tools/assets_extract.py` → `assets.py`。

- 修改 ROI、阈值、模板图片后，**必须同步更新** `image.json` 和 `assets.py`（重新生成），否则下次生成会被覆盖
- **ROI 语义**：`roi_front` = 匹配后的点击位置，`roi_back` = 搜索裁剪区域
- 匹配成功后 `roi_front` 会被更新为实际匹配位置
- 多文件支持：`image.json`, `image2.json`, `image3.json` 等均被读取

## 页面导航

`tasks/GameUi/page.py` 定义了一个以 `Page` 对象为节点的页面有向图：

- `page_main.link(button=I_MAIN_GOTO_EXPLORATION, destination=page_exploration)`
- `page_exploration.link(button=I_BACK_YOLLOW, destination=page_main)`
- `ui_goto(target_page)` 自动 BFS 寻路并逐跳点击
- 每个 `Page` 由唯一的 `check_button` 资产标识
- `page.additional` 列表用于全局弹窗拦截（自动关闭）

## 异常体系

```
TaskEnd              正常完成任务，task 标记 success=True
GameStuckError       游戏卡死（截图对比连续相同超出 stuck_timer=60s）
GameTooManyClickError点击超限（click_record 超 15 次仍未变化）
GameBugError         游戏客户端 bug，重启可恢复
GamePageUnknownError 未知页面（维护/网络）
GameNotRunningError  游戏进程不在运行
ScriptError          开发人员失误（exit(1)）
RequestHumanTakeover 请求人工介入（连续失败 3 次）
```

- `run()` 中的主循环捕获 `TaskEnd` 返回 `True`，其余异常触发重试或退出的调度逻辑
- 连续失败 ≥ 3 次：停止模拟器 + 推送通知 + `exit(1)`

## 重要约定

### Interval 统一为 3

云手机（分辨率 1280×720）下所有 `interval`、`sleep` 值统一为 **3** 秒。历史遗留值（0.2、0.5、1.5、2）需逐步迁移。改动时同步检查所有分支路径。

### Wait 方法的区别

- `wait_until_appear(target, wait_time=N)` — 等待目标出现（有超时）
- `wait_until_pos_stable(...)` — 等待目标**在同一位置**稳定（依赖 `roi_front` 变化检测）
- `wait_until_stable(target, timer, timeout)` — 等待目标**连续多次匹配成功**（不关注位置变化）

`wait_until_pos_stable` 有共享引用 bug（在并发/多次调用时 `roi_back` 会被修改），优先用 `wait_until_stable`。

### 多尺度匹配

`appear_multi_scale()` / `appear_then_click_multi_scale()` 用于自适配 HD 字体或 UI 缩放。
参数 `scale_range=(0.8, 1.2, 0.1)` 自动尝试多个缩放比例。使用成本较高，仅在新 UI / 字体变化时使用。

### 战斗奖励处理

`GeneralBattle.battle_wait()` 包含两种奖励检测路径（互斥）：
- **贪吃鬼路径**：检测 `I_GREED_GHOST` → 等待 `I_REWARD` → 循环点击收菜
- **奖励路径**：`I_REWARD` + `I_WIN` → 随机选择 `C_REWARD_1/2/3` 点击

`battle_wait()` 签名必须含 `false_button=None` 参数（所有重写已修复 12 处）。

### 好友邀请突发处理

`BaseTask._burst()` 在每次 `screenshot()` 时自动检测好友邀请界面，根据 `friend_invitation` 策略（accept/reject/jade_only）自动处理。接受勾协后立即触发 `WantedQuests` 任务。

## 已知陷阱

1. **Delegation 死循环**：`delegate_one()` 主循环无超时保护，遇到"已完成"委派页时无退出路径。所有 `while 1` 都应加 `Timer(N)` 兜底。

2. **Harvest 邮件循环**：`harvest_mail()` 仅靠邮件图标存在触发，庭院邮件图标常驻，导致反复进出邮箱。需加冷却或红点检测。

3. **RealmRaid 勋章检测**：`I_MEDAL_*` 的 `roi_back` 必须为全网格 `(140,129,1024,584)`，否则打完一个位置后 `find_one()` 返回 None 误判无结界。

4. **`I_MAIN_GOTO_EXPLORATION` 云手机**：`roi_back` 已扩至 `(350,70,650,150)` 以扩大搜索范围。云手机无法匹配时需重新截图。

5. **`I_FIRE_2` ROI**：寮突破进攻按钮 `roi_back` 已扩至 `(395,120,700,575)` 覆盖战斗区域。

6. **通知解析**：`dumpsys notification --noredact` 输出格式因 Android 版本而异。用 `NotificationRecord(` 分割 + `mCreationTimeMs=` 提取时间戳，避免依赖 `when=`。

7. **`c_shrine` 导航降级**：神社按钮在 `page_summon` 未到达时不应执行，需加状态检查。

## 调试经验

### while 循环必须有出口
- `wait_until_appear(target)` 不加 `wait_time` 会永久等待
- 所有循环加超时保护：`Timer(N).start()` + `reached()` 判断
- 修改 if/else 分支时，两个分支都要检查

### 追踪完整调用链
- 不要凭直觉判断"这个条件不会出现"
- 父类方法被覆盖、参数默认值、无超时调用是常见陷阱
- 通用方法（`GeneralBattle` 等）被多个任务复用时，修改前搜索所有调用点

### 错误日志
- `save_error_log()` 保存最近 60 帧截图 + 日志到 `log/error/<timestamp>/`
- 日志按日滚动（`loop()` 中 `date.today()` 检测）
- 时区强制 `Asia/Shanghai`（见 `server.py`）

## 环境

- 平台：**Windows**（部分功能依赖 `pywin32`）
- OCR：ppocr-onnx，通过 `module/ocr/rpc.py` 启动独立 ZeroRPC 服务（端口 22268）
- 设备连接：ADB + minitouch（首选）或 uiautomator2
- 容器：`docker compose up`（`network_mode: host`）
- **无测试框架**、无 lint/typecheck/pre-commit hooks

## 工具链

| 命令 / 用途 | 说明 |
|------|------|
| `pip install -r requirements.txt` | 安装依赖 |
| `python dev_tools/assets_extract.py` | 从 `res/*.json` 生成 `assets.py` |
| `python dev_tools/generate_requirements.py` | 用 `pip-compile` 生成 requirements.txt |
| `docker compose up` | 启动 Docker 开发环境 |
