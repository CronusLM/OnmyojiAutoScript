# OAS 错误修复 TODO

来源：`C:\Game\OAS\OAS\log\error\` 中 53 个报错日志分析（2026-06-16 ~ 2026-06-19）

## P0 - 核心阻塞（影响 25+ 次报错）

- [ ] **PAGE_MAIN_GOTO_EXPLORATION 云手机重新适配**
  - 根因：云手机 1280x720 下模板匹配不到或点击后页面不切换
  - 影响任务：RyouToppa（~14次）、Delegation（~6次）、RealmRaid（~3次）、DailyTrifles（~5次）、ExperienceYoukai
  - [ ] 在云手机 page_main 重新截图探索按钮
  - [ ] 更新 `res/image.json` 中模板和 ROI
  - [ ] 重新生成 `tasks/GameUi/assets.py`
  - [ ] 验证：`ui_goto(page_exploration)` 连续 10 次成功

## P1 - 稳定性修复

### OpenCV 模板尺寸崩溃（2 次）
- [ ] 找出所有资产中模板图片尺寸 > `roi_back` 裁剪区域的资产
- [ ] 修正 ROI 或重新截图
- [ ] 验证：`cv2.matchTemplate` 不再抛 `(-215)` 异常

### SwitchSoul 御魂切换失败（~7 次）
- [ ] `SS_SOUL_PRESET` 模板和 ROI 在云手机下校准
- [ ] `SS_SOU_TEAM_PRESENT` timeout 问题修复
- [ ] 验证队伍按钮点击准确性

## P2 - 死循环保护（5 次 TooManyClick + 已知问题）

- [ ] **`delegate_one()` 主循环加 `Timer(30)` 超时保护**
  - AGENTS.md 已知陷阱：遇到已完成委派详情页时无退出路径
- [ ] **`c_shrine` 导航失败降级** — RichMan 神社按钮在 page_summon 未到达时不应执行
- [ ] **`UTILIZE_UTILIZE_ZONES_GROUP` 超时保护** — KekkaiUtilize 导航失败后退出
- [ ] **`SAFE_RANDOM_CLICK` / "Click" 通用点击循环保护**

## 验证

- [ ] 连续运行 24h 无新错误日志生成
