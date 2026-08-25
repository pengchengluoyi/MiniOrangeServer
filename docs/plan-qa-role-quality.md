# 产品角色（需求分析 / 测试脑图 / 测试用例）质量与成本改造方案

> 触发案例：造好物【定制页工具链路优化】——跑了约 50 分钟、烧掉大量 token，产出的用例覆盖不全测试点。
>
> 涉及仓库：后端 `MiniOrangeServer`、前端 `MiniOrange`。

---

## 一、结论先行

1. **不是 prompt 写得不够细，恰恰相反**。三个角色 prompt 里已经把「造好物 / 传图定制 / 创意定制 / 定制页像商品详情页」硬编码进去了（`roles_catalog.py:136,143,257,314`），针对的就是这条需求，结果这条需求还是没写好。说明继续加 prompt 约束的边际收益已经为零。
2. **真正的失败路径是「静默降级」**：`draft_cases` 单次要一个 4096 token 的 JSON 里塞下 8 个测试点 × 最多 3 种情况 = 最多 24 条带步骤和预期的用例。这个输出几乎必然被截断；截断后 JSON 解析失败 → 整批 8 个点全部落到模板桩用例 `_stub_case`（`qa_role_jobs.py:835-838`）。桩用例长这样：「1. 打开应用 2. 按正向路径覆盖「X」 3. 核对页面展示与提示」。
3. **UI 会把桩用例算成「已覆盖」**（`qaProcess.js:coverageStats` 只看 `case_ids` 有没有值），所以界面显示覆盖率很高，人打开一看全是废话。这就是「跑了 50 分钟、覆盖不全」的真实体感来源。
4. **50 分钟是可以精确算出来的**，见下节。核心是全串行 + 每次重试都把之前的成果整体丢弃重做。

---

## 二、50 分钟的时间账

单次「重试脑图」并填了评论，后端会自动扩成三个 job（`qa_role_jobs.py:1241-1245`）：

| 步骤 | 参数出处 | 最坏墙钟 |
|---|---|---|
| `analyze_req` | `max_tokens=8192, timeout=180` (`qa_role_jobs.py:253-260`) | 180s |
| `draft_mindmap` | `max_tokens=8192, timeout=180` (`qa_role_jobs.py:299-306`) | 180s |
| `draft_cases` | 6 轮 × `timeout=90`，`CASE_DEADLINE_SEC=240` (`qa_role_jobs.py:784-842`) | 240–330s |
| `propose_atlas` | `max_tokens=4500, timeout=90` (`qa_role_jobs.py:1073`) | 90s |

一次带评论的重试 ≈ **9～11 分钟**。人看不满意再重试 3～4 次 ≈ 40 分钟；中间确认一次图谱骨架又会触发 `run_followup_pipeline` 把脑图和用例再跑一遍（`rAppAutomation.py:218-236`），凑到 50 分钟。

而且这 50 分钟里：

- 全程**同步 HTTP**（`rAppAutomation.py:484` `qa_process_tick`），前端硬等，超时设成 600000ms（`api/appAutomation.js:13`）。中途看不到任何进度，不能取消，进程崩了全丢。
- 服务端**没有任何并发**（全仓 `grep ThreadPoolExecutor|asyncio.gather` 零命中），而 `_fill_cases` 的 6 个批次之间完全独立，本来就该并行。
- 每次重试 `replace=True`（`qa_role_jobs.py:1293`），**把人已经看过、改过的用例整体删掉重写**。所以人的每一轮反馈都在从零开始。

---

## 三、根因清单（按影响排序，都带证据）

### R1 ★★★ 输出预算严重不足 → 截断 → 静默降级成模板桩

- `draft_cases`：`max_tokens=4096` / 批 8 个点。按每条用例（名称+模块+前置+3~5 步步骤+预期）约 200～300 token 算，8 个点铺开正向/异常/边界后是 15～24 条 = **4000～7000 token**。预算本身就不够。
- `llm_client.py` **没有截断修复**：`_extract_first_json_object` 靠括号配平（`llm_client.py:66-84`），截断的 JSON 一定配不平 → 返回 `None` → 整批丢失。
- `finish_reason == "length"` **没有被当成错误**，只记进 meta 就算了（`llm_client.py:106-112`）。
- 同样的问题在 `draft_mindmap`：8192 token 要输出「App/Web/端到端 三枝 × 多层模块 × 上百个测试点」的完整树，截断后落回规则树 `_tree_from_groups`（`qa_role_jobs.py:307-332`），UI 上仍然显示成「脑图」。

### R2 ★★★ 硬上限静默吃掉覆盖率

`CASE_BATCH=8`、`CASE_MAX_ROUNDS=6` → **最多只有 48 个测试点会被 LLM 处理过**；`CASE_DEADLINE_SEC=240` 通常让它连 6 轮都跑不满。剩下的点全部走 `_stub_cases_for_point`（`qa_role_jobs.py:840-841`），且**不上报**。

而脑图 prompt 明确要求「必须详尽，宁可多点也不许漏测」（`roles_catalog.py:249`），一条像定制页链路优化这样跨 App + 运营平台的需求，正常会出 80～150 个点。**上游被要求最大化产点，下游只能服务 48 个** —— 这是整条链最核心的结构性矛盾。

### R3 ★★★ 没有任何重试和 JSON 模式

`_post_chat_completions`（`llm_client.py:153-211`）：单次 `requests.post`，**429 / 5xx / 超时全部直接放弃**，没有退避重试；没有下发 `response_format={"type":"json_object"}`；90s 的读超时对一个 4096 token 的生成来说本身就偏紧，一次网络抖动 = 一整批用例变桩。

### R4 ★★☆ 用例编写者看不到需求原文

`_case_writer_context`（`qa_role_jobs.py:689-699`）只传 `title / journeys / new_features / keep_features / exceptions / surfaces / retry_note`。**没有 PRD 原文、没有 AC、没有已有用例做风格参照**。模型只拿到一个 40 字以内的测试点标题（`_short_title` 截断，`qa_role_jobs.py:390-394`）就要写出可执行步骤和可判定预期 —— 步骤空泛、预期不可判定是必然结果。

### R5 ★★☆ 失败不可见，覆盖率虚高

- 数据模型里**分不出 LLM 用例和桩用例**，`_stub_case` 产出的行和真用例长得一样。
- `coverageStats`（`qaProcess.js:307-313`）只要 `case_ids` 非空就算覆盖 → 桩用例把覆盖率刷满 → `coverReady` 放行进入下一步。
- 前端用例区是一张平铺表格（`QaProcessPanel.vue:1718-1746`），没有按测试点分组，没有「这条是模板兜底」的标记，只有整体「重试用例」一个按钮。人无法定点修，只能整体重试 —— 直接导致了第二节里的重试循环。

### R6 ★★☆ Token 浪费（不影响质量，纯白烧）

| 浪费点 | 位置 | 说明 |
|---|---|---|
| PRD 被发两遍 | `qa_role_jobs.py:88-105` | `_source_text` 拼 `source_text` 和 `understanding.source_excerpt`，后者是前者的 `[:20000]` 前缀（`qa_role_jobs.py:915`）。原文超过 20000 字时两个字符串不相等，去重失效，**同一份 PRD 拼进去两次**。 |
| 整棵图谱每次全量下发 | `app_atlas.py:763-776` | `compact_atlas` 不做任何裁剪，把所有模块/子模块/功能的 id + name + summary 全发。`analyze_req`、`draft_mindmap`、`propose_atlas` 三处都发。 |
| 共享上下文重复 6 遍 | `qa_role_jobs.py:805-813` | `_fill_cases` 每轮都重发完整 ctx + 80 条 `all_points` 标题 + 1.4k 字系统 prompt。 |
| 前缀缓存完全没命中 | `llm_client.py:184-190` | 变化的 `points` 插在 ctx 中间，导致每轮请求前缀都不同，Doubao / OpenAI 的 prefix cache 全部失效。 |
| `previous_mindmap` 全量回传 | `qa_role_jobs.py:293` | 重试时把上一版整棵树（含 detail）塞回去。 |
| 单模型干所有活 | `system_settings_service.py:494` | `case_execution_use` 是互斥单选，骨架生成、aspect 判定、校验这些轻活也在用写用例的强模型。 |

### R7 ★★☆ Prompt 过拟合到单条需求

三个 role prompt 里写死了造好物的业务事实：

- `roles_catalog.py:136` 「传图定制=上传本地图再下单（新）；创意定制=和 agent 对话出图（维持）」
- `roles_catalog.py:143` 「我的 → 定制模版页 → 定制页（像商品详情）→ 传图定制」
- `roles_catalog.py:257`、`roles_catalog.py:314` 同样的例子再各写一遍

这些是**应用知识，不是方法论**，应该作为数据从图谱/知识库传进去。写死在通用 prompt 里的后果是：换个 App 的需求，模型会往造物秀的形状上硬套。而且 prompt 里还在要求模型「输出前自检」（`roles_catalog.py:196`）—— 单次生成的自检是不可靠的，这件事必须交给代码。

### R8 ★☆☆ 没有增量、没有锁定

`artifact` 已经在算 `input_hash`（`qa_role_jobs.py:270,339,880`）但**从来没被用来跳过**。`draft_cases` 也没有 hash 短路（只有 `analyze_req` 有 `source_hash` 判断）。人工编辑过的用例没有 `locked` 标记，重试就被冲掉。

### R9 ★☆☆ 没有评测集

全仓没有 `test_*.py`，`scripts/verify_*.py` 是现有的验证脚本约定。**目前无法判断任何一次 prompt 或参数调整是变好了还是变差了** —— 这正是「不知道该怎么改」的根源。

---

## 四、改造方案

总思路：**把「一次生成一大坨」换成「分片生成 + 确定性校验 + 只补差」，把「静默降级」换成「显式失败 + 定点重试」。**

---

### P0 止血（1～2 天，不改架构，立刻见效）

#### P0-1 输出预算按内容量算，并加截断修复

`llm_client.py`：

```python
# 新增：截断抢救。finish_reason=length 时，丢掉最后一个不完整的数组元素后重解析
def _salvage_truncated_json(text: str, array_keys=("cases", "children", "points", "claims")):
    """把 {"cases":[{...},{...},{ 半条 } 截断成合法 JSON，保住前面完整的元素。"""
    ...

# call_chat_text 里
if meta.get("finish_reason") == "length":
    parsed = parsed or _salvage_truncated_json(content)
    meta["truncated"] = True   # 必须往上传，不能吞
```

`qa_role_jobs.py`：`max_tokens` 从常量改成按批量算。

```python
TOKENS_PER_CASE = 320          # 实测校准
def _case_batch_tokens(units: int) -> int:
    return min(8192, 800 + units * TOKENS_PER_CASE * 2)   # 2× 余量
```

#### P0-2 加重试 + JSON 模式 + 分离超时

`llm_client.py:_post_chat_completions`：

- `response_format={"type": "json_object"}`（provider 支持时；Doubao / OpenAI 均支持，不支持的降级忽略）。
- 对 `429 / 500 / 502 / 503 / 504 / ReadTimeout / JSON 解析失败` 做 2 次指数退避重试（1s / 3s，带 jitter）。**只重试这些，业务错误不重试。**
- `timeout=(10, read_timeout)`，`read_timeout` 按 `max_tokens` 估（约 `max_tokens / 25` 秒，下限 60s）。
- `finish_reason`、`retry_count`、`truncated` 落进 `dispatch_log`，设置页可见。

> 单这一条就能把「一整批用例变桩」的概率从目前的高频降到接近零。

#### P0-3 停止静默降级：桩用例必须被标记并上报

数据模型加两个字段：

```python
# 每条 draft_case
"origin": "llm" | "stub" | "human" | "import",
"locked": False,     # 人改过的置 True，replace 时不覆盖

# artifact.payload 加
"failures": [
    {"point_ids": ["tp12", "tp13"], "reason": "truncated", "detail": "finish_reason=length"},
],
```

`_stub_case` 产出的行一律 `origin="stub"`，并且**每次落桩都往 `failures` 里记一条**。

前端 `coverageStats` 改成三档：

```js
// qaProcess.js
const real    = points.filter(p => hasCase(p, c => c.origin !== 'stub')).length
const stubbed = points.filter(p => hasCase(p) && !hasCase(p, c => c.origin !== 'stub')).length
const gaps    = points.filter(p => !p.waived && !hasCase(p)).length
// coverReady：stubbed > 0 时不放行，提示「N 个点只有模板兜底，请补写或标本版不测」
```

#### P0-4 Token 瘦身（不动逻辑，纯省钱）

1. `_source_text` 去重改成前缀判断，消掉 PRD 双发：

```python
if s in seen or any(s.startswith(x) or x.startswith(s) for x in seen):
    continue
```

2. `compact_atlas` 加 `scope` 参数：只发与本需求 `hang.paths` 相关的子树 + 各层兄弟节点名（不带 summary）。整棵图谱只在 `propose_atlas` 时全发。
3. `_fill_cases` 的消息顺序改成 `[system] [不变的共享 ctx] [本批 points]`，让共享部分逐字节一致 → 命中 provider 前缀缓存。
4. `previous_mindmap` 只回传 `[{id, text}]` 清单，不回传整棵树。

**P0 预期收益**：桩用例率大幅下降；单需求 token 降 40～60%（PRD 双发 + 图谱全量 + 6 轮重复三项相加）；墙钟不变。

---

### P1 结构改造（约 1 周，这是解决「覆盖不全」的正题）

#### P1-1 脑图：三段式分片生成

替换现在的单次 8192 token 全树生成。

```
第 1 段  出骨架（不含测试点）
        输入：analysis bundle + 裁剪后的图谱路径
        输出：端 → 模块 → 功能 的枝，每枝一行
        max_tokens ≈ 1500，几乎不可能截断，可用便宜模型

第 2 段  并发填点（每个功能枝一个 call，6～8 路并发）
        输入：该枝的功能名 + 对应的 journey / new_feature / exception / PRD 证据片段
        输出：该枝下的测试点（正向 / 异常 / 边界）
        max_tokens ≈ 2000

第 3 段  确定性校验（纯代码，零 token）
        · surfaces 里每个端都有枝？
        · new_features 每个名字都有独立枝，且 focus=true 的有 ≥3 类 aspect？
        · keep_features 每个都有回归枝？
        · exceptions 每条都有对应异常点？
        · journeys 每条 entry→via→page 链路都有点？
        缺 → 只对缺的那几枝再发一次定向补齐 call（而不是重做整棵树）
```

要点：
- **把 prompt 里的「输出前自检」搬到代码里**。模型自检不可靠，代码校验 100% 可靠，而且能精确告诉人「运营平台的模型管理没有枝」。
- 分片后单次输出小 → 不截断；并发 → 墙钟从 180s 降到 ~40s。

新增文件建议：`server/services/ai/cover/mindmap_builder.py`、`server/services/ai/cover/checks.py`。

#### P1-2 用例：aspect 矩阵在代码里算，点级并发生成

现在是「模型自己决定每个点要写几条」。改成：

```python
# checks.py —— 复用并升级现有 _stub_cases_for_point 的关键词逻辑（qa_role_jobs.py:679-686）
def required_aspects(point: dict, analysis: dict) -> list[str]:
    """确定性地算出这个点必须有哪些情况。代码说了算，不问模型。"""
    aspects = ["正向"]
    if hits(point, ("上传", "保存", "下单", "提交", "支付", "发布")):
        aspects.append("异常")
    if hits(point, ("输入", "数量", "空", "格式", "大小", "列表", "上限")):
        aspects.append("边界")
    if hits(point, ("登录", "权限", "账号", "开关", "白名单")):
        aspects.append("权限")
    return aspects
```

- 生成单元 = `(point, aspect)`，不是 point。一个单元产出 1 条用例。
- batch = 3～4 个单元（约 1000 token 输出），**6～8 路并发**。
- 删掉 `CASE_MAX_ROUNDS` / `CASE_DEADLINE_SEC` 这种「跑不完就算了」的封顶，换成按单元总数算预算；**超预算必须把没写的点显式列进 `failures`**，绝不静默补桩。
- 生成后再跑一次确定性校验：每个点的 `required_aspects` 是否都有对应用例？缺的定点补。

墙钟估算：150 个单元 / 4 单元每批 = 38 批，8 路并发 × 每批 ~15s ≈ **1.5 分钟**（现在 6 轮串行只能覆盖 48 个点，要 4～5 分钟）。

#### P1-3 给每个测试点加 `evidence`（原文依据）

**这是单点性价比最高的改动。** 在 `analyze_req` 和脑图填点的输出 schema 里，每个 point 增加：

```json
{"id": "tp12", "text": "上传超过 10MB 图片有提示", "evidence": "PRD 3.2：单张图片不超过 10MB，超限提示…"}
```

一个字段同时解决四件事：

1. **用例编写有依据** —— `_case_writer_context` 把该点的 `evidence` 一起传下去，步骤和预期不再靠猜（直接修掉 R4）。
2. **幻觉可查** —— `evidence` 在 PRD 里搜不到的点，代码就能标出来给人看。
3. **人工审核可定位** —— 人看到点就知道对应 PRD 哪一段。
4. **可自动评测** —— 幻觉率变成一个能自动算的指标（见 P3）。

#### P1-4 并发基础设施

全仓目前零并发。加一个统一的小工具，别在业务代码里手写线程池：

```python
# server/services/ai/concurrency.py
LLM_MAX_WORKERS = 8   # 走 settings，可按 provider 限流调
def map_llm(fn, items, *, workers=None, on_partial=None) -> list:
    """有界并发 + 单项失败不拖垮整体 + 每完成一项回调（用于流式落库）。"""
```

注意 provider 侧限流：Doubao / OpenAI 都有 RPM/TPM 上限，`workers` 必须可配，并且 429 走 P0-2 的退避重试。

**P1 预期收益**：桩用例率 → 接近 0；覆盖率从「虚高的 100%」变成真实可信；单需求墙钟从 9～11 分钟降到 2～3 分钟。

---

### P2 人机协同与增量（约 3～4 天，解决「重试循环」）

#### P2-1 异步任务 + 进度可见 + 可取消

`qa_process_tick` 从同步改成投任务返回 `job_id`：

```
POST /app-automation/qa-process/tick/{app_id}   → { job_id }
GET  /app-automation/qa-process/job/{job_id}    → { phase, done, total, partial, failures }
POST /app-automation/qa-process/job/{job_id}/cancel
```

- 分片结果**流式落库**：每个功能枝、每批用例写完就存一次。人能一边看一边改；进程崩了不丢全部。
- 前端把 `timeout: 600000` 换成轮询（或复用现有 websocket 通道，`server/websocket/` 已有基础设施）。
- 进度条按 P1 的分片数算，是真进度不是假动画。

#### P2-2 定点重试，不再整体重做

- 前端用例区**按测试点分组展示**（现在是平铺表格），每个点一行「重写这个点」，每条用例一个「重写这条」。
- 桩用例显红，一键「批量补写这 N 个模板兜底」。
- 后端 `retryCover` 带评论时**不再连带 replace 掉全部用例**（去掉 `qa_role_jobs.py:1241-1245` 的自动扩散 + `replace=bool(retry_jobs)`）。改成：评论 → LLM 先判定「这条评论影响哪几个功能枝」→ 只重跑那几枝。
- `locked=True` 的用例永不被覆盖。
- 启用已经算好但没用的 `input_hash` 做短路。

#### P2-3 应用知识从 prompt 里搬出来

- 把 `roles_catalog.py` 里写死的造好物业务事实（`:136,143,257,314`）删掉，改成从图谱 summary / 已通过的应用知识（`knowledge_*` 服务已有）里作为数据注入。
- role prompt 只留方法论，长度砍掉 30～40%。
- 删掉 prompt 里的「输出前自检」要求（已由 P1-1/P1-2 的代码校验替代）。
- 负向指令（「禁止…」「不要…」）改成正向 checklist —— 负向约束在长 prompt 里遵守率明显更低。

#### P2-4 双模型分工

`system_settings_service.py` 的 `case_execution_use` 目前是**互斥单选**（`:494` 打开一个会关掉其他）。扩成按用途多选：

| 用途 | 建议档位 | 用在 |
|---|---|---|
| `cover_skeleton` | 便宜快 | 脑图骨架、aspect 判定、评论影响面判定 |
| `cover_content` | 强 | 用例正文、需求分析 |
| `case_execution` | 现状 | 真机执行链（不动） |

---

### P3 评测集（2～3 天，但**必须在 P1 之前搭起来**）

**没有这个，P0/P1/P2 的每一次改动都是盲改，也就无法回答「到底改好了没有」。**

按现有 `scripts/verify_*.py` 约定新增 `scripts/verify_cover_quality.py`。

**Golden set**：固化 3～5 条已经人工审过的需求，**造好物【定制页工具链路优化】必须在里面**。每条存：

```
docs/regression/cover-eval/
  zaohaowu-custom-page/
    prd.txt                # 需求原文
    points.golden.json     # 人工确认的测试点清单
    cases.golden.json      # 人工确认的用例清单
```

**自动指标**：

| 指标 | 怎么算 | 目标 |
|---|---|---|
| 测试点召回率 | 生成点 vs golden 点，语义匹配（embedding 或 LLM-judge） | ≥ 85% |
| 幻觉率 | `evidence` 在 PRD 里搜不到的点占比 | ≤ 5% |
| 端覆盖 | `surfaces` 每个端都有枝（代码判定） | 100% |
| 异常覆盖 | `exceptions` 每条都有对应异常点（代码判定） | 100% |
| aspect 完整率 | 每点的 `required_aspects` 都有用例（代码判定） | 100% |
| **桩用例率** | `origin == "stub"` 占比 | **0** |
| **截断率** | `finish_reason == "length"` 占比 | **0** |
| 单需求 token | `dispatch_log` 汇总 | ≤ 现状 40% |
| 单需求墙钟 | 同上 | ≤ 3 分钟 |

跑法：`python scripts/verify_cover_quality.py --set zaohaowu-custom-page`，结果追加到 `docs/regression/cover-eval/CHANGELOG.md`（跟 `docs/regression/CHANGELOG-session.md` 一个风格）。**改任何 prompt 或参数前后各跑一次。**

---

## 五、落地顺序与工作量

| 阶段 | 内容 | 工作量 | 依赖 |
|---|---|---|---|
| **第 0 步** | P3 评测集（先只做 golden set + 代码可判定的那几个指标） | 1.5 天 | 无 |
| **第 1 步** | P0-1/2/3 截断修复 + 重试 + 桩用例标记上报 | 1.5 天 | 无 |
| **第 2 步** | P0-4 token 瘦身 | 0.5 天 | 无 |
| **第 3 步** | P1-3 `evidence` 字段（含 prompt schema 改动） | 0.5 天 | 第 0 步（要能验证幻觉率） |
| **第 4 步** | P1-4 并发工具 + P1-1 脑图三段式 | 2 天 | 第 1 步 |
| **第 5 步** | P1-2 用例 aspect 矩阵 + 点级并发 | 2 天 | 第 4 步 |
| **第 6 步** | P2-1 异步 + 进度（前后端） | 2 天 | 第 5 步 |
| **第 7 步** | P2-2 定点重试 UI（前端为主） | 1.5 天 | 第 6 步 |
| **第 8 步** | P2-3 prompt 瘦身 + P2-4 双模型 | 1 天 | 第 0 步 |

**先做第 0～2 步（3.5 天）就能拿到肉眼可见的改善**：桩用例不再冒充真用例、token 砍掉一半、界面上第一次能看清「哪些点其实没写」。

---

## 六、涉及文件清单

**后端 `MiniOrangeServer`**

| 文件 | 改动 |
|---|---|
| `server/services/ai/regression/llm_client.py` | 重试、JSON 模式、截断抢救、分离超时、`finish_reason` 上报 |
| `server/services/qa_role_jobs.py` | 动态 `max_tokens`、`origin`/`locked`/`failures`、去掉硬上限、`_source_text` 去重、`_case_writer_context` 补 evidence/AC |
| `server/services/ai/cover/mindmap_builder.py` | **新增** 脑图三段式 |
| `server/services/ai/cover/case_builder.py` | **新增** 用例点级并发 |
| `server/services/ai/cover/checks.py` | **新增** 确定性校验 + `required_aspects` |
| `server/services/ai/concurrency.py` | **新增** 有界并发工具 |
| `server/services/ai/app_atlas.py` | `compact_atlas` 加 scope 裁剪 |
| `server/services/ai/roles_catalog.py` | 删业务硬编码、删「自检」、负向改正向、schema 加 `evidence` |
| `server/services/system_settings_service.py` | provider 用途从互斥单选改多选 |
| `server/routers/rAppAutomation.py` | tick 改异步 + job 查询/取消 |
| `scripts/verify_cover_quality.py` | **新增** 评测脚本 |
| `docs/regression/cover-eval/` | **新增** golden set |

**前端 `MiniOrange`**

| 文件 | 改动 |
|---|---|
| `src/utils/qaProcess.js` | `coverageStats` 三档（真实/桩/缺口）、`coverReady` 桩用例不放行 |
| `src/views/Testing/QaProcessPanel.vue` | 用例按测试点分组、桩用例显红、点级重写、真进度条、可取消 |
| `src/api/appAutomation.js` | tick 改投任务 + 轮询，去掉 600s 硬等 |
| `src/utils/dispatchLog.js` | 展示 `finish_reason` / 重试次数 / 截断标记 |

---

## 七、不建议做的事

- **不要再往三个 role prompt 里加约束**。已经试过了（造好物的例子就写在里面），无效。截断和硬上限是物理问题，prompt 解决不了。
- **不要靠换更强的模型**。截断、无重试、48 点上限、桩用例冒充真用例这四条，换任何模型都一样。
- **不要为了省 token 减少测试点**。要省的是重复下发的 PRD、全量图谱、6 轮重复的共享上下文（R6），不是覆盖面。
- **不要在没有评测集的情况下动 prompt**。改完无法判断好坏，只会再来一轮 50 分钟。

---

## 八、一句话总结

现在的链路是「让模型一次吐出全部答案，吐不完就悄悄换成模板，人看不出来，只能整体重试」。改造后是「代码切片、并发生成、代码校验、缺什么显式说出来、人定点补」—— 覆盖率从虚高变真实，墙钟从 50 分钟变 3 分钟，token 降一半以上。
