# 造好物「登录类」回归 — 问题与解决方案

包名：`com.mathmagic.zaohaowu`  
典型用例：`app-001` 一键登录、`手机号登录` 等。

## 期望执行模型（反应式守卫）

```
Plan 业务点击
  → miss（blocked_overlay / 定位失败）+ plan_attempt 截图
  → 守卫 · 隐私同意 → Tap 同意
  → 重试 Plan
  → miss → 守卫 · 系统权限 → Tap 仅在使用中允许
  → 重试 Plan → 成功
→ 下一步飞书步骤 / 预期校验
```

---

## 问题 1：系统权限守卫点不到「仅在使用中允许」

**根因**：阻塞屏一律 `blocked_overlay`，连守卫 label 也被挡；守卫仅 CLIP。

**方案**：`is_overlay_dismiss_target_label` + `tap_system_permission_on_engine`。

---

## 问题 2：阻塞屏上白跑 CLIP full grid

**方案**：业务 label + `is_screen_blocked()` → 立即 `blocked_overlay` miss。

---

## 问题 3：同意按钮双 Tap

**方案**：`tap_consent_agree_on_engine(single_tap=True)`。

---

## 问题 4：步骤 1 失败后仍跑步骤 2

**方案**：`action_ok=False` → `break`。  
**补充**：`action_ok` 现由 `business_step_results_ok` 决定，中间 `plan_attempt` 不算失败。

---

## 问题 5：锁屏 / 解锁截图与耗时

| 项 | 方案 |
|----|------|
| 截图 | `device_prep_lock`、`device_prep_unlocked` |
| 耗时 | `mAdb` 缩短上滑/PIN 等待（目标 15–20s，视 MIUI） |

---

## 问题 6：CLIP query 表

`server/services/locate/clip_query_plan.py`：同意、系统允许、empty checkbox、本机号码一键登录、手机号图标等。

---

## 问题 7：回放侧栏守卫排在业务 Plan 前（负 index）

**现象**：先显示「守卫 · 隐私同意」再显示「Plan - 勾选…」。

**方案**：守卫 `plan_index` 改为 `1xxx`；`flat_items` 按 `run_elapsed_ms` 交错。

---

## 问题 8：失败 Tap 截图显示成最后成功帧

**现象**：`plan_attempt` 无图，前端 fallback 到 `op.screenshot`。

**方案**：`capture_trace_frame` 写入 `plan_attempt`；前端失败 action 不回退整步截图。

---

## 问题 9：Tap 成功仍报「前置操作未成功，界面校验无效」

**根因**：`all(r.ok for r in step_results)` 把 `plan_attempt` 算进去。

**方案**：`business_step_results_ok` 用于 `action_ok` 与 `_check_expected`。

---

## 问题 10：预期「切换到手机号登录页面」失败

**现象**：图谱为「登录注册页」，屏上已是「手机号登录」标题。

**方案**：

- `normalize_page_intent` 区分「手机号登录页」
- `evaluate_dynamic_expectation` 的 `page_nav` 规则
- `pages_semantically_match` 手机号语义

---

## 问题 11：阻塞文案不明确

**方案**：`blocked_overlay_message` → `当前屏被{隐私同意弹窗|系统权限弹窗…}占用`。

---

## 验证清单

- [ ] 侧栏顺序：Plan → miss → 守卫 → Tap → 重试 → 成功
- [ ] miss 截图 = 当时弹窗
- [ ] 步骤 1 成功后执行步骤 2
- [ ] 手机号登录预期通过（OCR 含「手机号登录」）
- [ ] 阻塞文案含弹窗类型
- [ ] 全用例合理耗时（解锁 + 守卫重试仍可能 >2min）
