# OAS 仓库指南

## 项目概述

OAS（OnmyojiAutoScript）—— 阴阳师自动化脚本，Python 3.10+，Windows 平台。

## 入口点

| 命令 | 说明 |
|------|------|
| `python gui.py` | 桌面 GUI（Fluent UI，Qt/QML） |
| `python server.py` | Web 服务（FastAPI，默认 `0.0.0.0:22270`） |
| `python script.py` | 调度器自动使用，无需手动调用 |

## 目录结构

```
tasks/            — 每个功能一个子目录
  <Task>/           — 如 Orochi, RealmRaid, Delegation
    script_task.py    — 主逻辑
    assets.py         — 自动生成的资产定义（勿手动编辑）
    config.py         — pydantic 配置模型
    res/              — 模板图片 + image.json
  Component/        — 通用行为组件
    GeneralBattle/    — 战斗循环、胜负检测、奖励领取
    GeneralRoom/      — 创建/退出房间
    GeneralInvite/    — 邀请好友、等待队员
    GeneralBuff/      — 打开/关闭加成面板
    SwitchSoul/       — 御魂预设切换
module/           — 核心基础设施（设备、OCR、配置、服务器）
deploy/           — 安装部署相关
```

一个任务的 `ScriptTask` 类继承结构：`GameUi` + 所需 Component Mixin + 自己的 Assets。

## 资产系统

这是本项目最重要的约定。

- **数据源**：`res/image.json`（可带编号如 `image2.json`）。
- **生成目标**：`assets.py` 由 `dev_tools/assets_extract.py` 自动生成。
- **修改规则**：修改 ROI 或替换模板图片后，**必须同步更新** `image.json` 和 `assets.py`，否则会被覆盖。
- **ROI 含义**：`roi_front` 是匹配后的检测输出位置+点击位置，`roi_back` 是搜索区域（截图裁剪范围）。匹配成功后 `roi_front` 会被更新为实际匹配位置。
- **搜索范围**：`roi_back` 为全网格 `(140,129,1024,584)` 时，`find_one()` 的 `front_center()` 仍能正确计算分区索引（分区 ROI 用于比较）。

## Interval 约定

云手机适配：所有 `interval`、`sleep` 值统一为 **3**。已有历史遗留值（0.2、0.5、1.5、2 等）需逐步迁移。

## 页面导航系统

- `tasks/GameUi/page.py` 定义页面有向图，`ui_goto(page)` 自动寻找最短路径并逐跳点击。
- 链接按钮为 `I_MAIN_GOTO_*` 系列资产（在 `tasks/GameUi/assets.py` 中）。
- `I_MAIN_GOTO_EXPLORATION` 的 `roi_back` 已扩至 `(350,70,650,150)` 适配云手机。

## 已知陷阱

1. **`battle_wait()` 参数兼容**：`GeneralBattle.run_general_battle()` 传递 `false_button` 参数，所有重写 `battle_wait()` 的任务签名必须包含 `false_button=None`。当前已修复 12 处。

2. **RealmRaid 勋章检测**：所有 `I_MEDAL_*` 模板的 `roi_back` 需为 `(140,129,1024,584)`（全网格），否则打完一个位置后 `find_one()` 返回 None 导致误判无结界。当前 `I_MEDAL_0~5` 及 `I_MEDAL_3_2/3_3` 已扩。

3. **Delegation 死循环**：`delegate_one()` 的 `while 1:` 主循环没有超时保护。在遇到"已完成"委派详情页时（无 `I_D_START`），需加 `Timer(30)` 兜底退出。

4. **Harvest 邮件循环**：`harvest_mail()` 仅靠邮件图标存在就触发，庭院邮件图标始终存在，导致反复进邮箱。需加冷却或红点检测。

5. **通知解析**：`dumpsys notification --noredact` 输出格式因 Android 版本而异。用 `NotificationRecord(` 分割 + `mCreationTimeMs=` 提取时间戳，避免依赖 `when=` 字段。

6. **`I_FIRE_2` ROI**：寮突破进攻按钮 `roi_back` 已扩至 `(395,120,700,575)` 覆盖战斗区域。

## 环境配置

- 时区强制为 `Asia/Shanghai`（见 `server.py` 启动逻辑）。
- OCR 使用 ppocr-onnx（通过 `module/ocr/` 启动独立服务）。
- 设备连接：ADB + minitouch（首选）或 uiautomator2。

## 工具链

无 lint、typecheck、test 等标准工具链，无 pre-commit 钩子。

## 调试经验

### 1. while 循环必须有出口
- `while 1` + `appear` 等待条件时，必须加超时后备
- `wait_until_appear(target)` 不带 `wait_time` 会永久等待，所有调用都要检查
- 需要超时的地方用 `wait_until_appear(target, wait_time=N)` 或 `Timer(N).start()` + `reached()` 判断

### 2. interval 统一用 3s
- 与项目约定一致（见上），云手机环境下 1.5s 偏激进
- 改动时同步检查所有相关路径（if/else 分支都要改）

### 3. 追踪完整代码路径再下结论
- 不要凭直觉判断"这个条件不会出现"或"这个分支不会走到"
- 症状 ≠ 根因，必须追踪完整的调用链和数据流
- 特别留意：父类方法被子类覆盖、参数默认值、无超时调用

### 4. 分支修复要覆盖所有路径
- 修复 `if/else` 分支逻辑时，两个分支都要检查
- 新增参数要检查在所有调用处的影响
- 通用方法被多个任务复用时，修改前搜索所有调用点
