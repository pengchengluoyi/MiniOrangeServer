# 改造方案：大模型主导的 Agentic 执行引擎

> 目标：把用例执行从「**大模型盲规划一串固定 events → 规则代码逐条跑 → 失败文本盲 replan（上限3次放弃）**」，改造为「**大模型每步看真实屏幕 → 自主决定下一步 action → 执行 → 再看**」的闭环 agent，让大模型**整体负责分析与执行**。

## 决策定稿（D1–D6，已拍板）

| 决策 | 结论 |
|---|---|
| **D1 用例形态** | **全量改成「目标 + 检查点」**，废弃固定事件序列 |
| **D2 决策与定位** | **直接用大模型（VLM）能力看图给坐标，杜绝 locate VLM 两段式** |
| **D3 感知频率** | **每步都正常看图**（每步截图进决策） |
| **D4 成功判定** | **交给 VLM 断言** |
| **D5 请求人工** | **允许 agent 主动请求人工**；需设计前端对话流程（见 §4.6，前端当前零实现） |
| **D6 落地范围** | **仅 adb 通道**（有 UI 树、坐标更准，先跑顺） |


---

## 1. 背景：现状为什么不够用

以你刚跑的 `app-001（一键登录）@ 5fda2f6d` 为例，暴露了现状的结构性缺陷：

```
event1 launch_app(adb) PASS
event2 wait_screen_ready(vlm) FAIL: 当前是闪屏页，无一键登录按钮
event4 wait_screen_ready(vlm) FAIL: 当前是隐私协议弹窗
event7 wait_screen_ready(vlm) FAIL: 隐私协议弹窗
event10 wait_screen_ready(vlm) FAIL: 隐私协议弹窗
→ reached max_replans=3 → partial (6P/4F)
```

设备明明就停在**隐私协议弹窗**上——一个人看一眼就知道"点同意/进入"。但当前引擎做不到，根因链（研究结论）：

1. **两段式、非闭环**：`generate_overview()` 首次规划时**看不到任何截图**（`prompts.py:41` 明写"这一步没有截图"），只能凭用例文本 + 能力菜单盲排一串固定 events。
2. **replan 是文本盲改**：`replan_single_step()` 只拿到 `failure_summary` 一句话（"当前截图为…"），**截图不传给模型**（`orchestrator.py:277`）。模型看不见弹窗，只能再排一个 `wait_screen_ready` 期望回到原页面 → 再 FAIL。
3. **`wait_screen_ready` 名不副实**：`vlm_executor.py:102` 只 assert 一次、不轮询，页面没就绪立即 FAIL。
4. **硬上限**：`max_replans=3`（case 级累计），3 次拼接后仍未 PASS 就 `break` 放弃。
5. **大模型只当"规划器"不当"执行者"**：真正"看当前屏→决定下一步→执行→再看"的闭环**只存在于 persona 清缓存特例**（`ai_persona_executor.py:294` `_drive_clear_cache_iterative`），且为清缓存硬编码。主流程没有这种自适应能力。

**一句话**：引擎缺少一个"先感知实际页面、再决定下一步"的通用循环；大模型的眼睛（截图）只在定位/断言时睁开，规划与纠错时全程闭眼。

---

## 2. 现状架构（研究结论摘要）

```
/case-runner/run → case_runner.run_cases → _execute
  → build_run_context(探测 adb/remote/vlm/hitl 通道)
  → CapabilityRouter(全 case 复用)
  → run_case:
       generate_overview()   ← 大模型①  一次, 无截图, 出固定 events[]
       Orchestrator(plan).run():
         while i < len(events):
           router.dispatch(events[i])         ← 规则代码
             needs_vlm? capture+locate/assert  ← VLM(定位/断言)
             executor.execute()               ← adb/remote/internal/hitl/ai_persona
           FAIL → replan_single_step()         ← 大模型②  无截图, ≤3次
       compute_overall → pass/partial/fail
```

- **大模型介入点**：① 首次 plan（无图）② 失败 replan（无图）③ persona 展开（有图，唯一闭环特例）。
- **可复用的底座**（改造保留）：`CapabilityRouter` 路由/fallback、六个 executor、通道选择（指纹/adb/remote）、能力菜单（按连通性过滤 + cost 排序）、`capture_screen`、`case_memory`/trace 落盘、连通性闸门。
- **纯规则代码**（改造替换）：Orchestrator 主循环推进、replan 触发时机、`max_replans`、事件拼接重编号。

---

## 3. 改造目标与核心思想

**核心思想**：把 persona 那个"截图→看图→决策一步→执行→再看"的闭环，从特例**上升为主执行引擎**。用例从"一串固定 events"变成"一个**目标 + 可选检查点**"，由大模型每步看真实屏幕自主推进。

```
新执行循环 (AgentExecutor)：
  observation = { 截图(+adb UI树) , 目标 , 已执行动作历史 , 可用能力菜单 , 设备/通道信息 }
  loop step in 1..N:
    action = LLM.decide_next(observation)      ← 大模型每步看图决策 ONE action
    if action == DONE:      assert 目标达成 → PASS/FAIL 收尾
    if action == GIVE_UP:   → fail + 原因
    if action == ASK_HUMAN: → HITL
    result = router.dispatch(action)           ← 复用现有 executor/通道/定位
    observation = re-observe(截图 + 结果 + 历史追加)
    if 卡死/震荡/无进展/超预算: → 收尾
```

四条设计原则：
1. **每步有眼睛**：决策前必带当前截图（adb 侧附 UI 层级树，信息更全）。未预期页面（弹窗/权限/更新提示）模型能直接看到并处理。
2. **目标导向而非脚本导向**：用例文本 → `goal` + 可选 `checkpoints`（软锚点，用于判定进度/成功），不再是必须逐条命中的硬序列。
3. **底座全复用**：动作最终仍走 `CapabilityRouter → executor`，通道选择、定位、装包、shell、指纹合并全部沿用；改造只替换"决策层"。
4. **有界与安全**：步数预算 + 成功断言 + 震荡/无进展检测 + HITL 升级，取代脆弱的 `max_replans=3`。

---

## 4. 新架构设计

### 4.1 分层（改造后）

```
run_case:
  ┌─ [可选] rough_plan()  大模型出"粗路线/提示"(非强制序列, 供 agent 参考)
  └─ AgentExecutor(goal, checkpoints, ctx).run():        ← 新增, 替代 Orchestrator 主循环
       每步:  observe() → decide_next_action(VLM) → router.dispatch() → record trace
                              │
                              └── 复用: CapabilityRouter / executors / 通道 / 定位 / case_memory
```

### 4.2 决策模型：`decide_next_action`（新增，取代 replan）

输入（observation）：
- 当前**截图**（必带）；adb 通道附 `dump_hierarchy_xml`（可点元素 + resource-id + bounds，大幅提升定位准确率）。
- `goal`（用例目标文本）+ `checkpoints`（已过/未过状态）。
- **动作历史**（最近 K 步：action + 结果 + 简短观察），让模型知道"刚点了同意、现在应该到登录页"。
- **能力菜单**（复用 `available_menu_brief`，含每 cap 的 executor/cost/params schema）——模型只能从菜单选 action。
- 设备/通道信息（复用 `run_context.to_prompt_brief`，含 OEM 提示）。

输出（结构化，单步）：
```json
{
  "thought": "屏幕是隐私协议弹窗，需先点同意才能进入登录页",
  "action": {"capability_id": "tap_element", "params": {"x": 620, "y": 1840}},
  "expected_after": "进入一键登录页",
  "status": "continue" | "done" | "give_up" | "ask_human",
  "confidence": 0.9
}
```
- **D2 定稿：坐标由决策 VLM 直接给出**（看图即输出 `x,y`），**不再调 locate VLM**。`tap_element/swipe_element_to_element/long_press_element` 的 `params` 直接带绝对像素坐标，router 拿到坐标直接交 adb 执行（省一次调用、链路更短）。
- adb 侧把 `dump_hierarchy_xml` 的可点元素 bounds 一并给模型，辅助其给出更准的坐标。
- 幻觉防护：坐标必须落在屏幕范围内、capability 必须在菜单、params schema 校验（沿用 `_validate_events` 式硬校验），否则本步判无效并回灌"上一步动作非法"让模型重选。

### 4.3 感知：每步截图 + adb UI 树

- 复用 `capture_screen(force_fresh)`；adb 设备额外 `engine.dump_hierarchy_xml()`（RemoteEngine 无 UI 树，仅像素——这也是 adb 通道相对 ClawNode 的一个优势点，可在决策 prompt 里利用）。
- 缓存策略沿用现有（remote 0.9s 去重 + 动作后 invalidate）。

### 4.4 收尾与安全（取代 max_replans）

| 机制 | 说明 | 参考现有实现 |
|---|---|---|
| 步数预算 `max_steps` | 如 20 步硬上限，取代 case 级 max_replans=3 | 新增 |
| 成功断言 | `status=done` 时用 VLM `assert_visual` 确认目标达成才算 PASS | `planner.assert_visual` |
| 震荡检测 | 连续 N 步同 action/同屏无变化 → 判卡死 | persona `_drive_clear_cache_iterative` 已有 stuck/oscillation |
| 无进展检测 | checkpoint 长时间不推进 → 升级 replan-with-hint 或 HITL | 新增 |
| HITL 升级 | `status=ask_human` 或卡死 → `HitlExecutor` | 复用 |

### 4.5 与用例/记忆的衔接

- 用例 spec：新增从飞书用例文本抽 `goal + checkpoints` 的轻量解析（可先用大模型一次性抽取，复用现有 provider）。
- trace/case_memory：每步 observation+action+result 落 trace（比现在的 event 粒度更细，利于回放/调试/baseline 提炼）。baseline 从"事件序列"演进为"成功动作轨迹"，供后续同类用例做 few-shot 提示。

### 4.6 请求人工（D5）：Agent HITL 对话流程

**核心洞察（复用现状，不重造后端）**：agent 的"请求人工"本质就是发一个 `human_*` 能力动作——`human_confirm / human_choice_single / human_choice_multiple / human_input_text / human_upload_image / human_acknowledge`。这些能力已经由现有 `HitlExecutor` 完整接管：`compose_hitl_prompt`(LLM生成话术) → `submit_request(request_id)` → `transport.push_request` 广播 `hitl_request` → `wait_for_reply` 阻塞 → 前端 `POST /hitl/reply` 按 `request_id` 唤醒 → 答案写入 `ctx.shared['hitl_last_answer']`。

所以 agent 侧只需：决策模型输出 `status=ask_human` + 一个 `human_*` action → `AgentExecutor` 走 `router.dispatch` → 拿到人工答案 → **把答案作为观察的一部分回灌下一步**，继续循环。

```
decide_next_action → {status:"ask_human", action:{capability_id:"human_choice_single",
                       params:{question:"检测到未知弹窗，如何处理？", choices:["点同意","返回","跳过用例"]}}}
  → router.dispatch → HitlExecutor（现成：composer→push_request→阻塞→/hitl/reply→answer）
  → answer 写入 shared['hitl_last_answer']
  → AgentExecutor 读 answer 追加进 observation.history → 下一步 decide_next_action
```

**"多轮对话"如何实现**：每次 `ask_human` 是一次单轮 request/reply（现状后端 `_Session` 一问一答即销毁，够用）；**多轮由 agent 自身的动作历史提供连续性**——agent 记得"上一轮问了什么、人答了什么"，下一轮再问是新的 request。P0 无需改后端 session 模型。若将来要"同一气泡里连续追问"的聊天线程体验，再加 `conversation_id/turn_index`（§7 风险 3 标注为增强项）。

**后端要补的小缺口**：
1. **协议对齐**：广播帧用 `type:hitl_request`，前端管理 WS 按 `action` 分发收不到 → 前端改用 `mWebSocket`（原样广播）按 `res.type` 过滤，或后端广播时同时带 `action` 字段。
2. **图片上传落盘端点**：`human_upload_image` 的 answer 需要一个上传接口（落盘返回 path/url）——当前缺，P1 补。
3. **`/hitl/*` 鉴权**：现无鉴权，涉及人工输入应补 token 校验 + `request_id` 归属校验。

**前端要新建的（MiniOrange，当前 HITL 零实现）**：
1. **HITL 监听器**：在 `mWebSocket` 上监听 `type ∈ {hitl_request, hitl_revoke, hitl_resolved}`。
2. **6 种对话弹窗组件**（对应 `ui_kind`）：`yes_no_buttons / notice_with_ack / text_input / radio_list / checkbox_list / image_uploader`；用 `deadline_at` 做倒计时。
3. **回复链路**：`POST /hitl/reply {request_id, kind, answer, skipped?}`、`POST /hitl/skip`；断线重连 `GET /hitl/pending` 恢复未决请求。
4. **agent 执行页**：把 agent 的 `thought/action/observation` 逐步流式展示（配合 §4.5 的细粒度 trace），HITL 弹窗作为对话流的一环嵌入。

**下行 `hitl_request.data` 字段**（前端渲染依据）：`request_id, sn, run_id, case_id, kind, title, body, options[{id,label,hint}], constraints, created_at, timeout_sec, deadline_at, ai_reasoning, screenshot_path`。
**上行 `POST /hitl/reply` answer 按 kind**：confirm=bool、acknowledge="ack"、input_text=str、choice_single=选项id、choice_multiple=list[str]、upload_image={path,mime}。

---

## 5. 兼容与复用（改造边界）

**保留不动**：`CapabilityRouter`、六个 executor、通道选择器 `resolve_control_channel`、指纹合并、能力菜单/plugins、`capture_screen`、连通性探测与闸门、HITL、`case_runner` 的 Run 编排与状态统计外壳。

**替换**：`Orchestrator` 主循环 + `max_replans` 拼接逻辑 → `AgentExecutor` 闭环。

**改造**：`planner` 增 `decide_next_action`（带图单步决策，**直接出坐标，替代 locate/replan**）；`prompts` 增 agent system prompt（把现有 replan prompt 里那些 OEM/按键经验规则搬过来当"操作纪律"）。agent 模式下不再使用 `wait_screen_ready`（其"到达某页"的意图由每步看图天然覆盖）与 locate VLM。

**并存策略**：`AgentExecutor` 与旧 `Orchestrator` **并行共存**，用 `run_context` 或用例级开关 `execution_mode: plan | agent` 切换；**D6 定稿：先仅对 adb 通道设备启用 agent 模式**，remote/ClawNode 暂走旧 plan 模式，灰度验证、随时回退。

---

## 6. 分期实施（仅 adb 通道，D6）

- **P0（打通闭环，最小可用）**：新增 `AgentExecutor`（observe→decide→dispatch→re-observe + 步数预算 + VLM 成功断言 + 震荡检测）；`decide_next_action` 带图单步决策**直接出坐标**；用例增 `goal + checkpoints` 抽取；`execution_mode=agent` 仅对 adb 设备启用，跑通 `app-001`（自己点掉隐私弹窗进登录页）。
- **P1（感知/稳态/人工）**：adb UI 树注入决策；HITL 打通——后端补协议对齐+上传端点+鉴权，**前端建 6 种对话弹窗 + 监听 + 回复链路**（§4.6）；无进展升级 HITL；trace 细粒度落盘；OEM 经验规则迁入 agent prompt。
- **P2（记忆/提效）**：成功轨迹 baseline few-shot；成本优化（历史压缩/降频）；plan 模式对 adb 逐步下线，评估是否推广到 remote 通道。

---

## 7. 风险与权衡

1. **成本/延迟**：每步一次 VLM 决策，比现在（仅 needs_vlm 步截图）调用更密。缓解：动作历史压缩、只在"看图必要"时全图、小步合并、成功轨迹缓存。→ **决策点 D3**。
2. **非确定性**：agent 每次路径可能不同，回归"可复现性"下降。缓解：成功轨迹 baseline + 低温度 + checkpoints 约束。
3. **幻觉动作**：模型可能选菜单外/参数错的 action。缓解：沿用 `_validate_events` 式硬校验（capability 必须在菜单、executor 必须连通、params schema 校验）。
4. **OEM 知识流失**：现在 prompt 里硬编码的 MIUI/HyperOS 清缓存/装包路径不能丢，需搬进 agent 的"操作纪律"或做成可检索提示。
5. **回退保障**：agent 模式失败可自动回退旧 plan 模式（并存期）。

---

## 8. 决策定稿（D1–D6 已拍板）

- **D1**：用例全量改为「目标 + 检查点」，废弃固定事件序列。
- **D2**：决策 VLM 直接看图给坐标，**杜绝 locate VLM 两段式**。
- **D3**：每步都截图看图决策。
- **D4**：成功与否交给 VLM 断言。
- **D5**：允许 agent 主动请求人工（复用 `human_*` 能力 + 现有 HitlExecutor），**前端 HITL 对话 UI 需新建**（§4.6）。
- **D6**：仅 adb 通道启用 agent 模式，remote 暂走旧 plan 模式。

---

## 9. 验收标准

1. `app-001` 在 agent 模式下**自动识别并点掉隐私弹窗**，进入一键登录页，不再卡在 `wait_screen_ready` 循环。
2. 未预期页面（权限申请、更新提示、弹窗）能被 agent 看图后自主处理，而非直接失败。
3. 步数预算/震荡检测生效，不会无限循环，也不会 3 次就过早放弃。
4. 底座（executor/通道/定位/指纹/HITL）零改动复用；plan 模式可随时回退。
5. 每步 observation/action/result 有 trace，可回放定位问题。
