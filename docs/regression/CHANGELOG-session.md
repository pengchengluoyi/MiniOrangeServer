# 回归 / 守卫 / 回放 — 本轮改动总览

> 造好物 `com.mathmagic.zaohaowu` 飞书用例（一键登录、手机号登录等）联调期间的后端 + 前端改动摘要。  
> 代码以工作区未提交 diff 为准；本文仅描述设计与行为，不替代源码。

## 1. 核心设计变更：反应式 Overlay Guard

**旧模型（已废弃）**  
在业务 Plan 之前批量跑守卫；`plan_index` 用负整数把「守卫 · 隐私同意」插到业务 Plan 前面；回放侧栏先看到守卫再看到业务。

**新模型（当前）**  
每个业务点击走：**Plan → 点击 →（miss 且屏阻塞）→ 一轮守卫 Plan + 单次 Tap → 重试 Plan**。

| 项目 | 说明 |
|------|------|
| 触发 | `_run_mobile_click` 失败 + `is_screen_blocked()` |
| 每轮守卫 | 仅 `守卫 · {类型}` + 一次处置 Tap，无 Detect/Recheck 展示节点 |
| `plan_index` | 业务 `0..n`；守卫 `(step+1)*1000 + (round+1)*10`（如 `1011`、`1021`） |
| 展平顺序 | `build_operation_plan_tree` → `_build_flat_items_by_execution_order`，按 `run_elapsed_ms` 跨 Plan 交错 |
| 阻塞快速路径 | 非守卫 label + 阻塞屏 → `blocked_overlay`，跳过 CLIP full grid |
| 阻塞文案 | `blocked_overlay_message()` → `当前屏被{隐私同意弹窗\|系统权限弹窗…}占用` |

关键文件：`overlay_guard_service.py`、`copilot_service.execute_steps`、`app_automation_service.build_operation_plan_tree`。

## 2. 执行结果判定：`business_step_results_ok`

**问题**：`step_results` 含中间 `plan_attempt`（`ok=False`），导致 `action_ok`、预期校验 `all_steps_ok` 误判失败（Tap 已成功仍提示「前置操作未成功」）。

**规则**：每个业务 `index` 只取**最后一次**非守卫行；忽略 `index >= 1000` 与 `phase=overlay_guard`。

使用处：

- `feishu_regression_service._run_command_block` → `action_ok`
- `_check_expected` / `_verify_step_expected` → 是否允许做页面/文案断言

## 3. 回放与截图

| 改动 | 说明 |
|------|------|
| `plan_attempt` 行 | 点击 miss 时 `capture_trace_frame`，写入 `screenshot_before/after` |
| `flat_items` 动作匹配 | 带 `phase`、`click_attempt`、`guard_round`，避免动作与截图串台 |
| Plan 时间戳 | `flat_items` 内 Plan 节点带当次 `run_elapsed_ms` |
| 前端 fallback | 失败 action 不再回退到 `op.screenshot`（最后一步的图） |

## 4. 预期校验语义

| 改动 | 说明 |
|------|------|
| `normalize_page_intent` | 「手机号登录」单独归一为 `手机号登录页`，不与「登录注册页」混同 |
| `pages_semantically_match` | 预期含「手机号」时可与图谱「登录注册页」+ OCR「手机号登录」对齐 |
| `evaluate_dynamic_expectation` | 支持「切换到/进入 XXX 页面」→ OCR 含目标页文案即通过 |

## 5. 系统权限 / Consent 定位

| 改动 | 说明 |
|------|------|
| `is_overlay_dismiss_target_label` | 同意、仅在使用中允许、始终允许等在阻塞屏上仍可定位 |
| `tap_system_permission_on_engine` | hierarchy → OCR → 多通道 |
| `tap_consent_agree_on_engine(single_tap=True)` | 守卫模式单次同意，避免双 Tap |

## 6. 设备准备：锁屏解锁

`mAdb.ensure_screen_ready` / `_unlock_keyguard`：

- 上滑间隔、PIN 键 `exists` 超时、按键间隔缩短（约 30s → 目标 15–20s）
- 设备准备 trace：`device_prep_lock`（唤醒后锁屏帧）、`device_prep_unlocked`（解锁后帧）

## 7. 多通道定位（Locate）

新增 `server/services/locate/`：`resolver`、`arbitrator`、`clip_query_plan`（造好物登录链 query 表）、`icon_row` 等。  
`LOCATE_ARBITRATOR=1` 时 Copilot 走新管道。

## 8. 前端（MiniOrange）

| 文件 | 改动要点 |
|------|----------|
| `ExecutionReplayer.vue` | `flat_items` 展平、Before/After、多通道蒙层、失败分析、手动标注入库 |
| `FeishuRegression/index.vue` | 回放入口、用例表 |
| `FeishuRegressionPanel.vue` | 配置与运行 |
| `caseText.js` | 步骤/预期多行解析 |

## 9. 涉及文件清单（便于 Code Review）

**MiniOrangeServer（已跟踪修改）**  
`mAdb.py`、`copilot_service.py`、`feishu_regression_service.py`、`app_automation_service.py`、`expectation_semantic_service.py`、`page_navigation_service.py`、`regression_run_context.py`、`regression_capture.py`、`clip_locate_service.py`、`toggle_locate_service.py`、`case_precondition_service.py`、`feishu_service.py`、`rFeishuRegression.py`

**MiniOrangeServer（未跟踪 / 新增）**  
`overlay_guard_service.py`、`server/services/locate/*`、`docs/*`、`scripts/*`

**MiniOrange**  
`ExecutionReplayer.vue`、`FeishuRegression/*`、`CaseMultilineCell.vue`、`caseText.js`、`feishuRegression.js`

## 10. 验证清单（回归）

- [ ] 操作步骤 1 侧栏顺序：Plan → miss → 守卫 → Tap → 重试 Plan → 成功
- [ ] miss 步骤截图 = 当时弹窗，而非最后成功帧
- [ ] 步骤 1 最终成功后继续步骤 2
- [ ] 预期「切换到手机号登录页面」在 OCR 含「手机号登录」时通过
- [ ] `blocked_overlay` 文案含具体弹窗类型
- [ ] 锁屏解锁 < 20s（视 MIUI 响应）
