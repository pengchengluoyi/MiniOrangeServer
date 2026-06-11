# 阻塞弹窗守卫（Overlay Guard）

## 定位

守卫是 **Plan 执行链路中的反应式子方案**，不是独立脚本，也**不是**在业务 Plan 之前批量执行。

业务步骤点击失败且 `is_screen_blocked()` 时：

1. 记录 `plan_attempt`（含截图与 `blocked_overlay` 文案）
2. **Plan「守卫 · {类型}」** + **单次 Tap** 处置
3. **重试业务 Plan** — 仍阻塞则下一轮守卫（默认最多 3 轮）

**不做** Detect / Recheck 展示节点；内部 Assert 仅用于判定可否重试点击。

## 触发时机

| 场景 | 行为 |
|------|------|
| `execute_steps` click 循环 | miss + 阻塞 → `apply_reactive_guard_round` → 重试 `_run_mobile_click` |
| 阻塞屏业务定位 | 非守卫 label → `blocked_overlay`，跳过 CLIP full grid |
| 阻塞屏守卫 label | `is_overlay_dismiss_target_label` → 允许 hierarchy/OCR/多通道 |
| 预期校验前 | `_prepare_screen_for_verify` 可静默清障 |

## 阻塞文案

`blocked_overlay_message(engine)` 根据 `detect_blocking_overlay` 返回类型：

| type | 用户可见文案片段 |
|------|------------------|
| `consent` | 隐私同意弹窗 |
| `system_permission` | 系统权限弹窗 |
| `agreement` | 协议全文页 |
| `generic_overlay` | 应用弹层 |
| `screen_not_ready` | 屏幕未就绪 |

示例：`当前屏被隐私同意弹窗占用`。  
`locate_debug.overlay_type` 存机器可读 type。

## 弹窗类型与处置

| type | 处置 |
|------|------|
| `consent` | `tap_consent_agree_on_engine(single_tap=True)` |
| `system_permission` | `tap_system_permission_on_engine`（hierarchy → OCR → 多通道） |
| `agreement` | Back |
| `screen_not_ready` | `ensure_screen_ready` |

## plan_index 与回放顺序

- 业务步骤：`0`, `1`, …
- 守卫步骤：`(before_step+1)*1000 + (round+1)*10 + 1`，例如步骤 0 第 1 轮 → `1011`

`flat_items` 按 `run_elapsed_ms` **交错**展平，禁止再用负 index 把守卫插到业务 Plan 前。

示例：

```
Plan - 勾选「底部协议勾选框」          @ 00:00:30
Tap · 勾选… (miss)                    @ 00:00:30
Plan - 守卫 · 隐私同意弹窗            @ 00:00:43
Tap · 同意                            @ 00:00:43
Plan - 勾选「底部协议勾选框」          @ 00:01:34  （重试）
Tap · @(120,2246) ok                  @ 00:01:41
```

## 与预期校验 / action_ok 的关系

- 守卫行、`plan_attempt` **不参与** `business_step_results_ok`。
- 最终业务 Tap 成功 → 飞书步骤 `action_ok=True`，可继续下一步与预期校验。

## 代码入口

| 模块 | 说明 |
|------|------|
| `overlay_guard_service.py` | `apply_reactive_guard_round`、`blocked_overlay_message` |
| `copilot_service.execute_steps` | 反应式重试循环、`plan_attempt` 截图 |
| `page_navigation_service.py` | consent / system_permission 单次 Tap |
| `app_automation_service.py` | `_build_flat_items_by_execution_order` |

## 延伸阅读

- [执行全流程](../regression/execution-flow.md)
- [本轮改动](../regression/CHANGELOG-session.md)
