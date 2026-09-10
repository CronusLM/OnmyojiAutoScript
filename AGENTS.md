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

凡普通 override `battle_wait(self, random_click_swipt_enable, false_button=None)` 必须接受 `false_button`（现 13 处 override 均已含）。上游策略化任务（EvoZone、ActivityShikigami 走 `battle_wait_strategy`）不支持 `false_button`，勿给它们传。

### 好友邀请突发处理

`BaseTask._burst()` 在每次 `screenshot()` 时自动检测好友邀请界面，根据 `friend_invitation` 策略（accept/reject/jade_only）自动处理。接受勾协后立即触发 `WantedQuests` 任务。

## 已知陷阱

> 每条标注 [状态]：未修 / 已修+commit / 已落地（代码即约束，勿回改）/ 已过时。随代码演进定期核对，失效即删。

1. [仍适用] **Delegation 死循环**：`delegate_one()` 内嵌 `ui_click` 与委派后 `while 1` 仍无超时（Delegation/script_task.py）。`I_D_BACK`（召回/返回）是现成出口，命中即退出。所有 `while 1` 都应加 `Timer(N)` 兜底。

2. [已落地] **RealmRaid 勋章检测**：`I_MEDAL_*` 的 `roi_back` 必须保持全网格 `(140,129,1024,584)`，收窄会致打完一个位置后 `find_one()` 误判无结界。

3. [已落地] **`I_MAIN_GOTO_EXPLORATION` 云手机**：`roi_back` 现为 `(280,70,800,150)`（扩大搜索范围）。云手机无法匹配时需重新截图，勿回改小。

4. [已落地] **`I_FIRE_2` ROI**：寮突破进攻按钮 `roi_back` 为 `(395,120,700,575)` 覆盖战斗区域。

5. [仍适用] **通知解析**：`dumpsys notification --noredact` 输出格式因 Android 版本而异。当前实现在 GuildActivityMonitor/script_task.py（用 `when=` 正则），若通知时间戳异常再切 `mCreationTimeMs=` 方案，避免依赖 `when=`。

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

## 上游同步（merge upstream/dev）经验

### 上游方向与本地方向相反处，多数保持本地
- interval：上游倾向 0.8~2，本地铁律统一 **3**（云手机）。本地已在 base 后改过的 interval 行，三方合并会保留本地；净改动逐行以本地为准。
- 失败按钮：上游切 `battle_wait` 策略框架后默认只认 `I_FALSE`；本地依赖 `false_button`（RyouToppa `I_FALSE_2`/loser_sign、RealmRaid 走 `battle_wait_v2`），**勿**让策略 def 自动合入这些任务。
- RealmRaid 退四目标：本地 `index == 9`（先打第九格）vs 上游 `index == 1`，保持本地（6cf59a71）。

### 陷阱：自动合并不报冲突，但产生语义错配
- 例：RyouToppa 本地调用 `run_general_battle(false_button=self.I_FALSE_2)`，上游恰在文件另处加 `@battle_wait_strategy()` 的 `battle_wait` → git 自动合并无冲突，但策略 def 会静默忽略 `false_button`/`random_click_swipt_enable`（`battle_wait_with_strategy` 只认 `battle_wait_plan`/`options`），失败检测退回 `I_FALSE`，寮突破失败画面会卡死。
- **同步后除解决冲突文件外，必须审查所有自动合并任务里通用方法（`battle_wait`/`battle_wait_v2`/策略 def）的最终形态**，尤其本地有 `false_button` 或特殊资产依赖的任务。

### 校验流程（只读先行，避免返工）
1. 预演冲突：`git merge-tree --write-tree --name-only dev upstream/dev`（只写对象库不动工作区），提前拿到冲突文件清单。
2. 资产集合比对（防漏资产致 `AttributeError`）：`python dev_tools/sync_assets_check.py`（默认对比 upstream/dev，`--ref` 可指定）。
3. 净变化审查：`git diff --cached dev -- <file> | rg -v '^[+-].*interval='` 过滤 interval 噪音，聚焦语义变化。
4. 资产文件注释尾随空格来自上游/生成器，`git diff --check` 报警可忽略，勿手改生成文件（下次 `assets_extract` 会覆盖）。

### 其它
- 上游新增 `tests/`（pytest 回归），随 merge 带入本地；"无测试框架"仅指本地未配 CI/依赖。
- 上游带回 `.github/` agentic workflow 编译产物（`gh-aw`），随 merge 整体跟随，勿手改。

## Git 工作流

- 主干 `dev` 跟踪 `origin/dev`（本 fork）；同步源为 `upstream`（runhey/OnmyojiAutoScript）
- 上游合并全流程：
  1. `git fetch upstream dev`
  2. 只读预演冲突：`git merge-tree --write-tree --name-only dev upstream/dev`（git ≥2.38），提前拿到冲突文件清单
  3. 建议合并前建备份：`git branch backup/dev-pre-merge-<短hash>`（惯例见 backup/dev-pre-merge-014219c8）
  4. `git merge upstream/dev` 并解决冲突；**除冲突文件外必须审查自动合并文件里通用方法最终形态**（见"上游同步经验"）
  5. 校验：`git diff --cached --check`（生成文件尾随空格可忽略）、`python dev_tools/sync_assets_check.py`、对改动 `.py` 做 `py_compile`
  6. 提交信息沿用惯例：`Merge remote-tracking branch 'upstream/dev' into dev`
- 常规提交 message：`<type>(<Task/Module>): <说明>`（chore/fix/feat/docs）
- **push 前默认停在本地，由人工确认**；勿 amend 已推送提交

## 环境

- 平台：**Windows**（部分功能依赖 `pywin32`）
- OCR：ppocr-onnx，通过 `module/ocr/rpc.py` 启动独立 ZeroRPC 服务（端口 22268）
- 设备连接：ADB + minitouch（首选）或 uiautomator2
- 容器：`docker compose up`（`network_mode: host`）
- 本地**无测试框架**（无 pytest 依赖/CI）、无 lint/typecheck/pre-commit hooks；上游 dev 自建 `tests/`，`git merge upstream/dev` 会一并带入

## 工具链

| 命令 / 用途 | 说明 |
|------|------|
| `pip install -r requirements.txt` | 安装依赖 |
| `python dev_tools/assets_extract.py` | 从 `res/*.json` 生成 `assets.py` |
| `python dev_tools/sync_assets_check.py` | 比对 upstream 与本地 assets.py 资产集合（缺失即报错） |
| `python dev_tools/generate_requirements.py` | 用 `pip-compile` 生成 requirements.txt |
| `docker compose up` | 启动 Docker 开发环境 |
