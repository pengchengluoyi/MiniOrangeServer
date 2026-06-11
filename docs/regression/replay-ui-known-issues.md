# 回放 UI（ExecutionReplayer）— 已知交互问题

> 仅记录产品与交互层面的问题，供后续迭代；**本轮对话不改代码**。

## 信息架构

| 问题 | 描述 | 严重度 |
|------|------|--------|
| 规划日志 vs 执行顺序 | 右栏「规划日志」按 `plan_index` 列出守卫 Plan，看起来像「写死先跑守卫」；与左栏 `flat_items` 时间序不一致 | 高 |
| 中间失败永久标红 | `plan_attempt` 在侧栏显示 ✗，即使用例最终成功；易误解为「整步失败」 | 中 |
| 操作步骤 section 的 ok | 依赖 `op.ok`（`action_ok`）；与单条 action 的 ✓/✗ 可能不一致 | 中 |
| 预期区块全红 | 操作成功但预期失败时，「预期动作 N」整段标 fail，不易看出是断言而非 Tap 失败 | 中 |

## 截图与时间轴

| 问题 | 描述 | 严重度 |
|------|------|--------|
| Plan 节点共用截图 | `emitPlan` 用 plan 级 `screenshot`（常为首/尾 action），与当前选中 Plan 时刻不一定一致 | 中 |
| 胶片条匹配方式 | 按 `imgUrl(screenshot)` 反查步骤；无截图的 miss 步不出现在胶片条 | 中 |
| Before/After 门槛 | 需同时有 before+after；仅 `plan_attempt` 单帧时无对比视图 | 低 |
| 时间戳重复 | 重试 Plan 若未带 `flat_items.run_elapsed`，仍可能都显示首 action 时间 | 低（后端已部分修复） |

## 断言与失败分析

| 问题 | 描述 | 严重度 |
|------|------|--------|
| 图谱 vs OCR 分裂 | Information 写「当前页·登录注册页」，屏上已是「手机号登录」；用户不知信哪个 | 高 |
| 「前置操作未成功」文案 | 历史 run 或边缘 bug 时，与成功 Tap 矛盾（后端已修 `business_step_results_ok`） | 高（旧数据） |
| 自动失败分析 | 选中失败 action 即请求 `analyze-failure`，易刷屏、耗时长 | 中 |
| Assert 假阳检测 | `effectiveStepOk` 对 verify 有 `stepOperationFailed` 修正，但旧 trace 仍可能不一致 | 低 |

## 操作负担

| 问题 | 描述 | 严重度 |
|------|------|--------|
| 侧栏过长 | 单步多次守卫重试可产生 10+ 同级条目，无折叠 | 中 |
| Replay 自动播放 | 会经过无截图的 Plan 节点，中间空白 | 低 |
| 手动标注入口深 | 需失败 action + 有截图 + 开 annotate 模式 | 低 |
| 英文 Report 头 | 与中文步骤混排 | 低 |

## 建议改进方向（文档用，未实现）

1. 规划日志标注「运行时插入」或隐藏守卫条目，仅保留业务 planned_step。
2. `plan_attempt` 显示为「尝试 · miss」灰色，与最终 Tap 区分。
3. 预期失败时 Information 优先展示 OCR `page_nav` 结果，再展示图谱 label。
4. 胶片条按 `run_elapsed_ms` 索引，而非截图 URL 去重。
5. 操作步骤 section 展示「最终：成功/失败」与「尝试次数」摘要。
