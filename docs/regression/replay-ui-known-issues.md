# 回放 UI（ExecutionReplayer）— 已知交互问题

> v0.0.91+ 已在 `MiniOrange/src/components/ExecutionReplayer.vue` 中处理下列问题。

## 状态总览

| 类别 | 问题 | 状态 |
|------|------|------|
| 信息架构 | 规划日志 vs 执行顺序 | ✅ 业务 planned_step 与「运行时插入」分栏展示 |
| 信息架构 | `plan_attempt` 永久标红 | ✅ 灰色「尝试 · miss」，↻ 图标 |
| 信息架构 | 操作 section 摘要 | ✅ 展示「最终：成功/失败」与尝试次数 |
| 信息架构 | 预期区块全红 | ✅ 断言失败用琥珀色，标题标注「断言未通过」 |
| 截图 | Plan 共用截图 | ✅ Plan 取下一 action 的 before 帧 |
| 截图 | 胶片条 URL 去重 | ✅ 按 `stepIndex` / `run_elapsed` 索引 |
| 截图 | Before/After 单帧 | ✅ 仅一帧时显示「当前屏」 |
| 截图 | 重试时间戳 | ✅ 沿用 `flat_items.run_elapsed` |
| 断言 | 图谱 vs OCR 分裂 | ✅ 校验步优先展示 OCR / `screen_preview` |
| 断言 | 「前置操作未成功」 | ✅ 后端 `business_step_results_ok`；UI 假阳提示保留 |
| 断言 | 自动失败分析 | ✅ 改为手动「分析失败原因」按钮 |
| 操作负担 | 侧栏过长 | ✅ 子项 >5 时可折叠 section |
| 操作负担 | Replay 空白 Plan | ✅ Plan 节点 `playable: false`，播放跳过 |
| 操作负担 | 手动标注入口 | ✅ 失败 verify / plan_attempt 也可标注 |
| 操作负担 | 英文 Report 头 | ✅「回放报告」「播放」 |

## 实现要点（供维护）

1. **规划日志**：`planLogGrouped()` 将 `plan_index >= 1000` / `守卫 ·` 条目归入「运行时插入」，与左栏 `flat_items` 顺序一致。
2. **plan_attempt**：`isPlanAttemptStep` 控制样式；不计入 section 失败红色。
3. **胶片条**：`timelineShots[].stepIndex` 绑定 `activeIndex`，不再按 `imgUrl` 反查。
4. **断言 Information**：`currentPageContext.preferOcrFirst` 时先展示 `screen_preview`。
5. **折叠**：`collapsedSections` + `visibleSidebarSteps`，仅 operation / expected section 且子项 >5。

## 仍须关注（非回放 UI）

- 历史 trace 中 `evaluate_dynamic_expectation` 的 `delta_hint` 若仍用 `all(r.ok)`，与 `business_step_results_ok` 可能不一致（后端）。
- 无 `flat_items` 的旧 trace 仍走 Plan→actions 嵌套路径，守卫顺序可能与新版略有差异。
