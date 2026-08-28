# 改造进度

分支：`feat/qa-role-generalize`（未提交，你的 3 个 WIP 文件改动原样保留）

方案文档：`plan-qa-role-quality.md`（v1）、`plan-qa-role-quality-v2.md`（v2 + 附录 A 审计）

---

## 已完成并验证

### 1. LLM 客户端加固（v1 P0-1 / P0-2）
`server/services/ai/regression/llm_client.py`

| 改动 | 说明 |
|---|---|
| 截断抢救 `_salvage_truncated_json` | 只在**完整元素边界**切，绝不留残缺元素（残缺元素会伪装成真用例把覆盖率刷高） |
| 指数退避重试 | 429/5xx/超时重试 2 次，带抖动；业务错误不重试 |
| JSON 模式 | `response_format`（有 schema 走 json_schema，否则 json_object）；provider 返回 400/422 时自动摘掉该字段重试一次，不会因为加 JSON 模式把原本能用的 provider 打挂 |
| 分离超时 | `(connect=10s, read=max(调用方值, max_tokens/25+30))`，老调用方不改参数也能自动拿到足够读超时 |
| 失败可归因 | meta 新增 `fail_kind`(http/parse/truncated) / `truncated` / `salvaged` / `attempts` / `retry_reasons` / `json_mode_downgraded` |

验证：`scripts/verify_llm_json_salvage.py` — 12/12 通过，其中 8 个场景是现有实现救不回、抢救逻辑救回来的。

### 2. 用例生成不再静默降级（v1 P0-3）
`server/services/qa_role_jobs.py`

- **动态输出预算**：按本批预计要写多少条算 `max_tokens`（原来固定 4096 要装 8 个点铺开后的 15~24 条，必然截断）
- **拆半重试**：没写全先把批次减半重试（截断几乎总是「一次要太多」），重试用尽才落桩
- **删掉硬上限**：`CASE_MAX_ROUNDS=6` 去掉（原来最多只有 48 个测试点会被模型处理过，其余静默变模板桩）。`CASE_DEADLINE_SEC` 改成 900s 安全阀，撞到必须把剩余点写进 `failures`
- **数据模型**：每条用例带 `origin`(llm/stub/human/import) 和 `locked`；artifact 带 `failures` / `aspect_gaps` / `stats`
- **按情况驱动覆盖**：`_point_aspect_gaps` 判据从「这个点有没有任何用例」改成「该有的情况齐不齐」，并把 `need_aspects` 带进 prompt
- **replace 保住人工改动**：`locked` 用例不再被重试删掉
- 导入的用例标 `origin=import` + `locked`（`cover_import.py`）

### 3. 顺手修掉的两个真 bug
- **测试点 id 不一致**：`_norm_points` 用 `id or point_id`、`_sync_points_from_mindmap` 用 `point_id or id`，同一个脑图叶子算出两个 id → `apply_cases` 永远匹配不上 → `case_ids` 恒为空 → **每个测试点都显示成「没挂用例」**，`coverReady` 直接卡住。这就是界面上的「用例覆盖不全测试点」。
- **点级 aspect 被跳过**：一个点只要有任一用例就整体跳过，所以人工只写了正向的点，异常/边界永远不会被生成 —— 覆盖率满格，实际缺一半。
- 附带：清掉 `case_ids` 里已不存在的 draft id（原来会残留，让覆盖率虚高）

验证：`scripts/verify_case_coverage.py` — 6 个场景全通过（正常 / 截断拆半 / 一直失败 / 截断救不回 / locked 保留 / id 对齐端到端）。

### 4. Token 瘦身（v1 P0-4）
- `_source_text` 去重改成看包含关系 —— 原文超 20000 字时 `source_text` 和它自己的前缀会被当成两份，**同一份 PRD 发两遍**
- 稳定/易变分离：图谱、用例库、共享上下文单独成一条 message 且 `sort_keys` 保证逐字节一致 → 命中 provider 前缀缓存（原来变化的 points 插在中间，缓存全不命中）
- `previous_mindmap` 全树改成只回传 `[{id, text}]`
- 用例编写者补上 `ac` / `baseline` / `delta` / `source_excerpt`（原来**没有需求原文也没有 AC**，只拿到 40 字截断的点标题就要写步骤和预期）

### 5. 前端：覆盖率说真话
`src/utils/qaProcess.js`、`src/views/Testing/QaProcessPanel.vue`

- `coverageStats` 三档：真实覆盖 / 只有模板兜底 / 完全没用例；模板兜底**不算**覆盖
- `coverReady` 在有模板兜底或情况缺口时不放行（原来放行，界面显示 100%）
- 面板：真实覆盖数、模板兜底红色告警、情况缺口清单、生成失败清单（截断/解析失败/模型报错/撞安全阀）、用例表新增「来源」列 + 桩用例行高亮 + 🔒 锁定标记、脑图退化成规则树时显式提示

构建通过（`npx vite build`）。

### 6. A 类去耦：应用事实搬进 ui_profile（v2 附录 A，第 1 步纯搬家）
新增 `server/services/ai/app_profile.py` + `server/resources/app_profiles/zaohaowu.yaml`

- 三层：通用常量（`GENERIC_*`，零业务词）→ 每应用 YAML 种子 → 运行期覆写（`merge_override`）
- contextvar 绑定（和 `dispatch_log.bind` 同一套），深层调用点不用逐层传参
- 绑定点：`case_runner._execute`（有 package）、`copilot_service.plan_message`（context 里有 package）

去掉业务字面量的文件：

| 文件 | 原来写死的东西 |
|---|---|
| `case_precondition_service.py` | 登录判定的 4 个 tab + 阈值 3 + logged_in_pages |
| `shared/page_context/page_context_service.py` | 首页识别的 7 个 tab、协议页标记、分段 tab 断言特判 |
| `local/navigation/page_navigation_service.py` | `BOTTOM_TAB_LABELS` / `SEGMENT_TAB_LABELS`、协议页文案、品牌文案（4 处） |
| `copilot_service.py` | `_SEGMENT_TAB_NAMES` 6 个 tab |
| `shared/semantic/expectation_semantic_service.py` | 分段 tab 断言特判 |
| `figma_logic_service.py` | `TAB_LABELS` |
| `local/locate/clip_locate_service.py` | 底栏 / 分段 tab 清单 |
| `app_automation_service.py` | 那个叫 `generic_markers` 却装着具体应用欢迎语的常量 |

**行为一致性已证明**（`scripts/verify_ui_profile.py`，全通过）：
- 造好物：8 个屏幕的已登录/首页判定与搬家前的硬编码基准**逐条一致**
- 未知应用：保留通用主导航词的识别能力（搬家前未接入的应用就是靠 首页/消息/我的 偶然识别的，不能弄丢），但不借用别人的专属 tab
- 同一屏在两个应用下判定不同 —— 证明画像真的生效了
- 未绑定时降级到通用默认，不崩不乱判

**防回流守卫**：`scripts/verify_no_app_literals.py` 从各应用画像取业务字面量（已排除「首页」「我的」这类通用词），扫通用服务。基线 **87 处 / 11 文件 → 48 处 / 3 文件**。建议进 CI。

---

### 7. 脑图 ⇄ 图谱对齐 P0（`plan-mindmap-atlas-learning.md`）

新增 `atlas_align.py`（归一化 + 模糊 + 术语表反向守卫）、`atlas_from_mindmap.py`（反推骨架，跳过端层、测试点只计数）；`app_profile` 加 `GENERIC_SURFACES` + `surfaces`；`cover_import` 走对齐层并按确定性分流合并 / 入 patch；前端 `placeBranch` / `mergeChild` 改成 id 优先。

验证：`scripts/verify_atlas_align.py`、`scripts/verify_mindmap_to_atlas.py`，全通过。

---

## 未完成

进度已于 2026-08-25 重新核对过实际代码，下表是核对后的状态。

### ~~最高优先：搬家只搬了一半~~ —— 已修

发现的问题：`roles_catalog.py` 里的应用事实已被摘掉（换成「列表页 / 详情页 / 本地上传提交 / 对话生成」这类通用占位），词进了 `zaohaowu.yaml` 的 `lexicon`，但**没有任何地方把它注入回 prompt** —— `archetype_of()` 零调用，`lexicon` 唯一消费点是 `atlas_align` 的模糊守卫。对造好物而言模型已经不知道「传图定制 = 上传本地图片后下单」「创意定制 = 与 agent 对话生成」是两条要分开测的路径。通用化的收益拿到了，应用化的补偿没给，而 `verify_no_app_literals` 照样全绿。

修法：`UiProfile.facts_prompt()` 把应用名、端、主导航、术语表拼成一段（造好物 322 字），由 `_ask_json` 统一注入，排在 stable 之前 —— 事实比图谱更少变，缓存前缀才切得干净。未接入画像的应用返回空串，不拿别人的事实冒充。

顺带补齐了画像绑定：contextvar 不跟着线程走，`_followup_in_background` / `_reanalyze_in_background` 两个后台线程原来没绑，`qa_process_assist` 也没绑（它连 `_get_app` 的结果都丢了）。这三条路径上的 prompt 本来都拿不到应用事实。

守卫：`scripts/verify_app_facts_injected.py`。它守的是 `verify_no_app_literals` 管不到的另一半 —— 后者只保证「摘走」，摘走之后没人补回来它一样全绿。新脚本逐条校验术语进了 prompt、排序正确、未接入应用为空、以及**每个会调模型的入口都绑了画像且 reset 了**。

### P2-2 定点重试（同步半截）—— 已做

以前点「重试脑图」只要填了评论，后端会自动扩散成 analyze_req → 脑图 → 整表 replace 用例，一轮 10 分钟。

现在 `jobs` 写了就只跑写了的那些。重试用例默认扔掉模板桩再补缺口，已有真用例和锁定用例都不动；表上每条有「重写」，告警上有「只补写这些」。脑图换了之后，挂在已消失测试点上的未锁定用例会清掉，避免覆盖率被孤儿搅乱。

守卫加在 `verify_case_coverage.py`：补写模板不动真用例、定点重写只动指定点、重试脑图不再扩散。

### P1 分片并发 —— 已做

- `server/services/ai/concurrency.py`：`map_llm` 有界并发，父线程 `copy_context()`，单项失败不拖垮其余。
- 脑图：骨架按端并发 → 薄枝并发填点 → `cover.checks.gaps` 只记 failures 不静默补。
- 用例：一轮缺口快照 → `map_llm` 批次 → 串行合并 → 截断拆半重试。

守卫：`verify_concurrency.py`、`verify_mindmap_shards.py`（顺带 `verify_case_coverage` 仍绿）。

### P2-1 异步 tick + 进度 + 可取消 —— 已做

- `server/services/qa_process_jobs.py`：内存任务表，`done/total` 按分片累加，`flush` 流式回写 `qa_process`。
- API：`POST .../tick` → `{job_id}`；`GET .../job/{id}`；`POST .../job/{id}/cancel`。同应用同时只跑一个（409）。
- 前端：`runQaProcessTick` 轮询；面板进度条 + 取消；CasesWorkbench「继续分析」同样可取消。

守卫：`verify_qa_process_jobs.py`。画像绑定点改到 `_tick_in_background`（`verify_app_facts_injected` 已跟）。

### 脑图 ⇄ 图谱 P1 别名学习 —— 已做（学习闭环）

- `m_atlas_alias` + `atlas_alias_repo`：approved 灌进对齐器，rejected 拦模糊同一对。
- 模糊对齐合并进建议目标写进 `after`，但仍进 patch；`aliases` 随 patch 落盘。
- 人审确认/驳回写别名表；`AtlasChangeReview` 展示「脑图 X → 图谱 Y」。
- 再次导入走 `how=alias` 可直接合并，不再进人审。

守卫：`verify_atlas_alias_learning.py`。未做：LLM `atlas_from_mindmap` job。

### 脑图 ⇄ 图谱 P2 反向同步 —— 已做

- `relink_mindmap`：按 `atlas_ref` 改名、回填 path、删掉的节点标 `orphan`，回来后清 orphan。
- 确认/保存图谱后 `relink_all_mindmaps` 写回所有需求脑图。
- 前端脑图列表 / 看板标「已失联」；图谱页可管理别名（启停/删除）。

守卫：`verify_relink_mindmap.py`。

### 其余

| # | 内容 | 规模 | 现状（已核对） |
|---|---|---|---|
| 1 | **v2 L1/L2/L3 + 组装器** | 大（约 8 天） | 字面量已清干净、守卫全绿，但**分层本身一个都没建**：`resources/methodology`、`resources/knowledge`、`prompt_assembler.py`、`page_archetypes` 均不存在。「清干净」和「建好」是两回事，现在只是把应用知识删了 |
| 2 | `page_profiles.yaml` per-app scope | 中（1 天） | 字面量已清。overlay 机制仍未做，`page_profiles.py` 还是全局单例 |
| 3 | `clip_query_plan.py` | 小 | **已完成**，字面量已搬进画像 |
| 4 | **B 类枚举收口** | 中（1 天） | 后端**已完成一半**：端的枚举和别名统一到 `app_profile.GENERIC_SURFACES`。前端还写死三处：`appAtlas.js` 的 `MIND_PLATFORMS`、`:495` 的 `['app','web','e2e','App','Web','端到端']`、`:561` 的 `order`；aspect 关键词表前后端各一份且已不一致，未动。要由后端接口下发 |
| 5 | v1 P1 分片并发 | 大（4 天） | **已完成**：`concurrency.map_llm`（copy_context）+ 脑图三段式（骨架/填点/代码校验）+ 用例并发批次。守卫：`verify_concurrency` / `verify_mindmap_shards` / `verify_case_coverage` |
| 6 | v1 P2 异步 + 定点重试 | 大（3.5 天） | **已完成**：定点重试（同步半截）+ 异步 tick（`qa_process_jobs`，POST→job_id / GET / cancel，前端轮询进度条可取消）。守卫：`verify_qa_process_jobs` |
| 7 | C 类 UI 文案 | 小（1 天） | 未动 |
| 8 | 评测集 golden data | — | **需要你提供**：人工确认过的测试点和用例清单。这是领域判断，编不出来 |
| 9 | 脑图 ⇄ 图谱 P1 别名表学习 | 中（2 天） | **已完成（学习闭环）**：`m_atlas_alias` + repo；确认/驳回写 approved/rejected；模糊合并进建议目标但必须人审；`AtlasChangeReview` 展示别名建议。守卫：`verify_atlas_alias_learning`。LLM job `atlas_from_mindmap` 未做 |
| 10 | 脑图 ⇄ 图谱 P2 反向同步 | 中（2 天） | **已完成核心**：`relink_mindmap` / `relink_all_mindmaps`（改名同步 + orphan）；accept/save 后跑；前端「已失联」标记；别名列表/启停/删除 API + 图谱页管理。守卫：`verify_relink_mindmap` |

### 交付状态

两个仓库的改动**全部未提交**：server 在 `feat/qa-role-generalize` 分支 35 个文件，client 在 `main` 12 个文件。

### 已知副作用
- `_source_text` 去重修复会改变 `understanding.source_hash`，下次 tick 时**每条需求会重跑一次 analyze_req**。一次性，且修完的分析质量更好。
- `figma_logic._tab_labels()` 现在返回底栏 + 分段（原 `TAB_LABELS` 只含第一个分段 tab），Figma 页面归类会多认出 `AI创意` / `想要成真` 两个 tab 页 —— 更正确，但属于行为变化。
