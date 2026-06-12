# 飞书用例预期校验（Expectation Assert）

## 与守卫的区别

| 层级 | 时机 | 依据 | 示例 |
|------|------|------|------|
| **守卫** | 业务点击 miss 且屏阻塞 | 弹窗是否消除 | Tap · 同意 |
| **用例预期** | 飞书「预期 N」列 | 操作完成后的界面/文案 | 「切换到手机号登录页面」 |

守卫保证**能继续操作**；预期判断**业务是否正确**。

## 前置操作是否成功

`_check_expected` / `_verify_step_expected` 使用 **`business_step_results_ok(step_results)`**：

- 每个业务 `index` 取**最后一次**结果
- **忽略** `plan_attempt` 中间失败、守卫行（`index>=1000`、`phase=overlay_guard`）

因此：**中间 miss 不影响预期校验**，只要最终 Tap 成功。

若仍提示「前置操作未成功，界面校验无效」：

1. 检查是否旧 run 数据（修复前跑的）
2. 检查最终业务行是否 `ok=False`（如 `stop_on_failure` 后无成功行）
3. 检查复合指令多 index 是否有一个失败

## 用例文本 LLM 解析（统一入口）

模块：`server/services/case_text_semantic_service.py`

| 飞书列 | 函数 | LLM 做什么 |
|--------|------|------------|
| **前置条件** | `parse_precondition_items` | 拆条 + 分类 `kind` / `phase`（清缓存、SIM、已登录…） |
| **测试步骤** | `parse_numbered_field(..., step)` | 按编号拆步骤，保留完整可执行语义 |
| **测试步骤（执行）** | `normalize_step_command` | 单条步骤 → Copilot 指令（补全点击/滑动等动词） |
| **预期效果（列）** | `parse_numbered_field(..., expected)` | 与步骤编号对齐的预期条目 |
| **预期效果（校验）** | `parse_expectation_claims` | 单条预期 → 原子断言（OCR/图谱校验） |

开关（任一关闭则全链路规则回退）：

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `CASE_TEXT_PARSE_LLM` | 继承 `EXPECTATION_PARSE_LLM` | 总开关，优先于后者 |
| `EXPECTATION_PARSE_LLM` | `1` | 预期原子断言是否走 LLM |
| `EXPECTATION_SPLIT_COMMA` | 关 | 规则模式下预期是否在逗号处切分 |

需配置：`LLM_API_KEY` + `LLM_API_BASE`（可选 `LLM_MODEL`）。

飞书同步时步骤/预期列即走 LLM；执行时前置条件、步骤规范化、预期校验共用同一套配置。

## 预期拆解（LLM + 规则）

一条「预期 N」先经 `parse_expectation_claims` 拆成原子断言，再逐条 `_check_expected`：

| 方式 | 说明 |
|------|------|
| **LLM** | 理解并列/从属，避免在逗号处误拆 |
| **规则回退** | 仅在 `；;、` 等处拆分，且两侧都含断言关键词；**默认不按逗号切** |

操作规划（`copilot_service.plan_message`）仍是 regex + `copilot_semantic` Tab 展开；**用例列解析**与 **预期原子断言** 走 `case_text_semantic_service` / `expectation_semantic_service`。

## 校验流水线

`_verify_step_expected` → `parse_expectation_texts` → `_check_expected`（每条 claim）：

1. `business_step_results_ok` — 不通过则短路，附当前页图谱信息
2. `evaluate_dynamic_expectation` — 文案/数量/导航类
3. `enrich_check_with_page` — 图谱/Figma + `judge_navigation_expectation`
4. OCR 子串 / 关键词片段

## 动态预期（`evaluate_dynamic_expectation`）

| 预期表述 | 判定 | method |
|----------|------|--------|
| 变成/显示为「XXX」 | OCR 含 XXX | `text_change` |
| 不包含「XXX」 | OCR 不含 | `text_absent` |
| 数量为 N | OCR 数字 | `numeric` |
| 原来是 X 现在是 Y | 界面含 Y | `state_change` |
| **切换到/进入 XXX 页面** | OCR 含「XXX」或「XXX页」 | `page_nav` |
| +1 / 增加1 | 操作成功且界面有数字 | `delta_hint` |

## 页面语义（`expectation_semantic_service`）

- `normalize_page_intent`：「进入 app 首页」→「首页」；**「手机号登录」→「手机号登录页」**（不与「登录注册页」混同）
- `pages_semantically_match`：预期「手机号登录」可与图谱 label「登录注册页」+ 屏上「手机号登录」标题对齐
- `judge_navigation_expectation`：规则 + 可选 LLM

**注意**：图谱识别为「登录注册页」、OCR 已为「手机号登录」时，应依赖 `page_nav` 或语义匹配，勿仅看图谱 label。

## 回放展示

```
预期动作 · 步骤 1
  Plan - 校验切换到手机号登录页面
  Assert - 界面已进入「手机号登录」
```

`checks[].reason` 显示在 Information 面板。

## 已知局限

| 问题 | 说明 |
|------|------|
| `delta_hint` 内 `steps_ok` | 仍用 `all(r.ok)`，与 `business_step_results_ok` 不一致（边缘场景） |
| 图谱未单独建「手机号登录页」节点 | 依赖 OCR `page_nav` 或语义规则 |
| 校验前 `_prepare_screen_for_verify` | 可能再清一层弹窗，与操作结束屏不一致 |

## 相关代码

| 文件 | 职责 |
|------|------|
| `feishu_regression_service._check_expected` | 单条预期入口 |
| `app_automation_service.business_step_results_ok` | 业务成败聚合 |
| `expectation_semantic_service.py` | 动态/导航语义 |
| `page_context_service.enrich_check_with_page` | 图谱/Figma |
