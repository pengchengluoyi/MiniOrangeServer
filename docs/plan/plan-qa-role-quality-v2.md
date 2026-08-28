# 产品角色改造方案 v2：方法论 Prompt + 插口注入

> v1 见 `docs/plan-qa-role-quality.md`。v2 **不替代 v1**，而是重做 v1 里关于 prompt 的部分。
>
> - v1 的 **P0（截断抢救 / 重试 / 桩用例显式化）** 和 **P3（评测集）** 完全保留，且仍是最先做的两件事。
> - v1 的 **P1（分片生成 + 确定性校验）** 保留，但校验规则的来源从「代码里写死的 if」改成「知识库数据驱动」。
> - v1 的 **P2-3「prompt 瘦身」被本文档整节取代**。

---

## 一、问题重述

现在 `REQ_ANALYST_SYSTEM_PROMPT` / `MINDMAP_WRITER_SYSTEM_PROMPT` / `CASE_WRITER_SYSTEM_PROMPT`（`roles_catalog.py:126-339`）把三类东西搅在一个字符串里：

| 类型 | 例子 | 应该在哪 |
|---|---|---|
| **方法论**（通用、跨应用不变） | 「按用户走完这件事的流程拆」「叶子才是测试点」「优化需求要同时覆盖原主流程回归 + 每个改点 + 改点带来的异常」 | ✅ 留在 prompt |
| **测试设计知识**（通用、可枚举、会持续增长） | 「有上传就必须有格式/大小/失败/取消/权限/超时/损坏」「详情页前面必有列表入口」 | ❌ 应该是**知识库数据** |
| **应用事实**（每个应用不同） | 「传图定制=上传本地图再下单」「创意定制=和 agent 对话出图」「定制页像商品详情页」「运营平台有模型管理、定制专区」 | ❌ 应该是**应用画像数据** |
| **契约**（结构，可机器校验） | 那一大坨 JSON 示例 | ❌ 应该是 **JSON Schema** |

后两类现在被硬编码在 prompt 里（`roles_catalog.py:136,143,168-175,257,314-315`），直接后果：

1. **换应用就失准**。一个小程序电商、一个桌面工具、一个 SaaS 后台，跑同一份 prompt 会被"传图定制/创意定制/我的→定制模版页"往造好物的形状上带。`platform` 枚举写死成 `app/web/e2e`，遇到「小程序」「桌面端」「开放 API」直接无处安放。
2. **只能靠加字兼容**。每接一个应用、每漏一个场景，就往 prompt 里再补一句「例如…」「不要…」。prompt 单调递增，且新补的约束会稀释旧约束的遵守率 —— 长 prompt 里的负向指令遵守率本来就低。
3. **示例值污染输出**。JSON 契约是用**真实业务值**举例的（`"entry": "我的"`, `"name": "传图定制"`, `"page_like": "商品详情页"`）。模型学到的不只是形状，还有值的倾向。换应用时它会照着写类似的名字。
4. **契约不可校验**。是一段散文里的 JSON 示例，代码没法拿它做校验，也没法下发给 provider 的 structured output。
5. **改动不可归因**。prompt 是一个大字符串常量，改了一句话导致效果变差，没有版本、没有 diff 记录、没法回滚到"上周那版"。

---

## 二、目标架构：三层 + 插口

```
┌─────────────────────────────────────────────────────────────┐
│ L1  方法论层 Methodology         通用，跨应用不变，很少改       │
│     · 你的职责与思考顺序                                       │
│     · 判定规则（用变量指代，不举业务例子）                       │
│     · 指向契约，不内联示例值                                    │
│     文件：ai/prompting/methodology/*.md                       │
├─────────────────────────────────────────────────────────────┤
│ L2  知识层 Library + Overlay      通用可枚举知识，持续增长       │
│     · page_archetypes    页面原型 → 必测维度                   │
│     · exception_playbook 能力 → 必覆盖异常                     │
│     · risk_dimensions    风险维度                             │
│     · test_design_rules  等价类/边界/状态迁移等设计手法          │
│     内置一份默认 + 每个应用可**增量覆写**                        │
│     文件：ai/prompting/library/*.yaml                         │
├─────────────────────────────────────────────────────────────┤
│ L3  应用层 App Profile            每个应用一份数据              │
│     · surfaces        这个应用有哪些端 + 别名映射（决定枚举）     │
│     · lexicon         业务术语表                              │
│     · atlas           应用图谱（已有）                          │
│     · conventions     入口习惯、登录态模型、环境                 │
│     · few_shot        该应用自己已审通过的样例                   │
│     存储：App.automation.qa_profile（或独立表）                 │
└─────────────────────────────────────────────────────────────┘
                            ↓
              ┌──────────────────────────┐
              │  PromptAssembler（代码）   │
              │  按 job 选插口 → 裁剪预算   │
              │  → 稳定序列化 → 记录用量    │
              └──────────────────────────┘
                            ↓
                    实际下发的 messages
```

**核心机制：库可以很大，注入必须很小。**

内置库里可以有 40 种页面原型、200 条异常清单 —— 但**一次调用只注入本需求命中的那 2~3 条**。这是"通用而不变重"的唯一办法。命中判定由第三节的能力打标决定，不靠模型记，也不靠 prompt 堆。

---

## 三、插口定义

### 3.1 插口清单

| 插口 id | 内容 | 来源 | 用于 job | 预算 |
|---|---|---|---|---|
| `output_contract` | JSON Schema（枚举按应用生成） | 代码 + L3 surfaces | 全部 | 400 |
| `app_profile` | 应用是什么、端有哪些、登录态/环境模型 | L3 | 全部 | 300 |
| `surface_vocabulary` | 端取值 + 别名映射表 | L3 | 分析、脑图 | 150 |
| `domain_lexicon` | 命中的业务术语（不是全表） | L3 | 全部 | 300 |
| `atlas_scope` | 与本需求相关的图谱子树（裁剪） | L3 atlas | 分析、脑图 | 800 |
| `capability_tags` | 本需求命中的能力标签 | 打标步骤 | 全部 | 80 |
| `page_archetypes` | 命中原型的必测维度 | L2 + 覆写 | 脑图、用例 | 400 |
| `exception_playbook` | 命中能力的必覆盖异常 | L2 + 覆写 | 脑图、用例 | 400 |
| `risk_dimensions` | 命中的风险维度 | L2 + 覆写 | 分析、脑图 | 200 |
| `test_design_rules` | 命中的设计手法 | L2 + 覆写 | 用例 | 300 |
| `requirement` | 本条需求原文 + 结构化字段 | 需求实例 | 全部 | 4000 |
| `prior_art` | 已有用例风格样例 / 上一版点清单 | 用例库 | 用例、脑图重试 | 600 |
| `few_shot` | 本应用已审通过样例 | L3 | 分析、脑图 | 700 |
| `human_feedback` | 人的评论 | 用户输入 | 全部 | 400 |
| `scope_hint` | 本次只处理哪一枝 / 哪几个点 | 分片器 | 脑图分片、用例分片 | 300 |

### 3.2 关键设计：契约本身要按应用生成

这是最能说明问题的一个点。现在契约里写死：

```
"platform": "app|web|e2e"
```

`platform` 的合法取值**是应用属性，不是通用常量**。所以契约不能是常量字符串，必须由 assembler 用 L3 的 `surfaces` 生成：

```python
# prompting/contracts.py
def analyze_req_contract(profile: AppProfile) -> dict:
    plats = [s.kind for s in profile.surfaces]          # 例：["app","web","e2e"]
    #                                                    # 另一个应用：["miniapp","desktop","openapi","e2e"]
    return {
        "type": "object",
        "required": ["summary", "change_kind", "journeys", "surfaces", "ac", "points"],
        "properties": {
            "change_kind": {"enum": ["new", "optimize", "unknown"]},
            "points": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["id", "kind", "text", "path", "platform", "evidence"],
                    "properties": {
                        "id":       {"type": "string", "pattern": "^tp\\d+$"},
                        "kind":     {"enum": profile.point_kinds},   # 默认 正向/异常/边界，应用可加
                        "text":     {"type": "string", "minLength": 8, "maxLength": 40,
                                     "description": "可观察、可判定的一句话"},
                        "path":     {"type": "array", "items": {"type": "string"},
                                     "description": "图谱路径，必须能对上 atlas_scope 里的节点名"},
                        "platform": {"enum": plats},
                        "evidence": {"type": "string",
                                     "description": "需求原文里支撑这个点的原句；推断出来的写「推断：…」"},
                    },
                },
            },
            # ...
        },
    }
```

一个 schema 换来三件事：
1. **下发给 provider 做 structured output**（Doubao / OpenAI 都支持 `response_format: json_schema`），从根上大幅降低格式错误。
2. **代码可校验**（`jsonschema`），不合规直接定点重问，不用整批丢弃。
3. **不再用业务值教形状**。字段含义靠 `description`，不靠 `"entry": "我的"`。

需要示例时走 `few_shot` 插口 —— 用**这个应用自己已审通过的**样例。新接入的应用还没样例时，用一份**中性占位样例**（`"<模块>"`、`"<页面>"`、`"<能力>"`），不给任何真实业务名。

### 3.3 能力打标：决定注入什么

新增一个极便宜的前置步骤，输出很小（几十 token），走便宜模型：

```python
# prompting/tagging.py
CAPABILITY_TAGS = [
    "upload", "download", "payment", "auth", "permission", "form_input",
    "list_paging", "search_filter", "media_play", "share", "push_notify",
    "im_chat", "ai_generate", "order_flow", "review_moderate", "config_backend",
    "cross_surface_sync", "offline_cache", "location", "camera", "third_party_login",
]

def tag_requirement(req, profile) -> dict:
    """输出 {"tags": [...], "archetypes": [...], "surfaces": [...]}。
    先用确定性关键词/图谱匹配打一遍，只有拿不准的才问一次便宜模型。"""
```

打标结果同时驱动四件事，一处产出多处复用：

1. **注入过滤** —— 只注入 `tags` 命中的 `exception_playbook` / `test_design_rules` / `risk_dimensions` 条目。
2. **确定性校验** —— v1 P1-1/P1-2 的校验规则不再是写死的 if，而是「打到 `upload` 标 ⇒ playbook 里 upload 的 7 条异常必须各有对应测试点，缺哪条报哪条」。
3. **aspect 矩阵** —— v1 P1-2 的 `required_aspects(point)` 由标签查表得出，不再是关键词硬编码。
4. **评测归因** —— 失败可以按标签聚合：「凡是打到 `cross_surface_sync` 的需求，端到端点召回率都低」。

> 注意：这一步把 v1 里 `_stub_cases_for_point` 那段关键词逻辑（`qa_role_jobs.py:679-686`）从「兜底生成器」升级成「知识库驱动的校验器」，同一份规则同时用于生成引导和事后校验。

### 3.4 知识库：内置 + 应用增量覆写

```yaml
# prompting/library/page_archetypes.yaml   —— 内置默认，通用
archetypes:
  - id: list
    label: 列表页
    must_cover: [空态, 首屏加载, 翻页或下拉, 单项进入下一级, 排序或筛选生效]
  - id: detail
    label: 详情页
    requires_upstream: [list, search, deeplink]     # 详情页必有上游入口，代码校验
    must_cover: [主信息完整, 主动作可执行, 返回后状态保持, 分享或收藏]
  - id: editor
    label: 编辑器/创作页
    must_cover: [新建, 保存草稿, 放弃编辑, 重进恢复, 提交结果可见]
  - id: form
    label: 表单页
    must_cover: [必填校验, 格式校验, 长度边界, 提交失败可重试]
  - id: backend_config
    label: 后台配置页
    must_cover: [新增, 编辑, 停用或删除, 列表可见, 配置对客户端生效]
```

```yaml
# prompting/library/exception_playbook.yaml
upload:
  label: 上传
  must_cover:
    - {scene: 格式不支持,   need: 明确拦截并说明支持格式}
    - {scene: 超出大小上限, need: 明确提示上限值}
    - {scene: 上传中取消,   need: 可取消且不产生脏数据}
    - {scene: 网络失败,     need: 有失败态且可重试}
    - {scene: 权限被拒,     need: 引导去设置，不崩不卡}
    - {scene: 超时,         need: 有超时反馈，不无限转圈}
    - {scene: 文件损坏,     need: 有可理解的错误提示}
payment: {...}
auth: {...}
cross_surface_sync:
  label: 跨端同步
  must_cover:
    - {scene: A 端改动后 B 端可见, need: 说明期望时延}
    - {scene: B 端不可见时的兜底,  need: 有刷新或重试入口}
```

```yaml
# 某应用的覆写：App.automation.qa_profile.library_overlay
page_archetypes:
  add:
    - id: ai_chat_canvas
      label: 对话式创作页
      must_cover: [首轮出图, 多轮修图, 生成失败重试, 结果落库可用]
exception_playbook:
  upload:
    add: [{scene: 触发内容审核, need: 有审核态提示且可申诉}]   # 增量，不替换内置 7 条
```

**这就是回答"不断兼容会不会让 prompt 越来越重"**：接新应用、补新场景，改的是 YAML 数据，且只在命中时注入；方法论 prompt 一个字不动。

### 3.5 应用画像

```yaml
# App.automation.qa_profile
version: 3
app:
  name: 造好物
  what_it_is: 以 AI 定制实物商品为主的电商应用
  login_model: 手机号 + 微信；未登录可浏览，下单需登录
  envs: [测试, 预发, 正式]
surfaces:
  - {kind: app,  label: App,      aliases: [客户端, 移动端, iOS, Android, 安卓]}
  - {kind: web,  label: 运营平台, aliases: [后台, 管理端, CMS, 运营后台, H5, 网页]}
  - {kind: e2e,  label: 端到端,   aliases: [跨端], meaning: 需要在两个端各看一次结果才算验证}
conventions:
  entry_habits: [我的, 首页, 消息, 发现]
  entry_rule: 入口必须来自需求原文或图谱；不得默认首页
lexicon:
  - {term: 造物秀,     means: 用户发布的作品动态流}
  - {term: 定制页,     archetype: detail, means: 单个可定制商品的详情与下单页}
  - {term: 定制模版页, archetype: list,   means: 从「我的」进入的可选模版列表}
  - {term: 传图定制,   means: 上传本地图片后下单}
  - {term: 创意定制,   means: 与 agent 对话生成图片后下单}
few_shot:
  analyze_req: [{req_id: "...", approved_at: "...", output: {...}}]   # 人审通过后自动入选
```

注意：**造好物的所有业务事实原封不动搬到这里** —— 不是删掉，是搬家。质量不靠"prompt 里少写点"，靠"该知道的仍然知道，但按应用取"。

`lexicon` 里的 `archetype` 字段是关键连接点：术语一旦标了原型，`page_archetypes` 的 `must_cover` 和 `requires_upstream` 就自动对这个应用生效。原来 prompt 里那句「定制页若类似商品详情页，前面必有模版/列表入口」（`roles_catalog.py:256`）就变成了 `{term: 定制页, archetype: detail}` 加内置的 `requires_upstream: [list,...]`，**并且从"祈求模型记住"升级成代码校验**。

---

## 四、方法论 prompt 长什么样

改造后的 `req_analyst.md`（对比原来 `roles_catalog.py:126-196` 约 2600 字）：

```markdown
你是测试需求分析师。你把一条需求读成「真实用户怎么用这个产品」，再拆出验收标准和测试点。

【输入】
你会收到若干带标题的上下文块。它们是本次任务的唯一事实来源：
- 【应用画像】这个应用是什么、有哪些端、端的别名、入口习惯、登录态模型
- 【术语表】本需求涉及的业务名词的含义
- 【图谱范围】与本需求相关的已确认产品结构
- 【能力标签】本需求命中的能力
- 【必覆盖异常】命中能力按规范必须覆盖的异常场景
- 【页面原型】命中页面类型按规范必须覆盖的维度
- 【风险维度】本需求需要关注的风险面
- 【需求原文】
- 【人的反馈】（可能没有）

上下文块之外的产品事实不要编。画像和术语表没写的能力，不要假设存在。

【思考顺序】
1. 这个需求属于新建还是改造？改造就先还原原有能力（baseline），再写这次的改动（delta）。没有把握的写「推断：…」。
2. 涉及哪些端？逐个对照【应用画像】的 surfaces 和别名表。原文里出现别名，就归到对应的端。
3. 每个端上，用户从哪进、经过哪几页、到哪一页做这件事？入口只能来自原文或【图谱范围】。
4. 哪些能力是这次新增（要作为独立功能测），哪些明确维持原逻辑（要回归，不能被新功能盖掉）？
5. 【必覆盖异常】和【页面原型】里的每一条，在这条需求上成立吗？成立的必须落成测试点；不成立的忽略。
6. 需要跨端验证结果的，单独标出来，不要在两个端各写一遍同一条链路。

【测试点要求】
- 一条测试点是一个可观察、可判定的结果，不是一段操作。
- 每条测试点必须带 evidence：原文里支撑它的原句；推断出来的写「推断：…」。
- 按功能铺开，不自我限制条数。

【输出】
严格按【输出契约】给的 JSON Schema 输出，不要 Markdown，不要任何解释文字。
契约里的枚举值只能取列出的那些。
```

对比原版的变化：

| | 原版 | v2 |
|---|---|---|
| 字数 | ~2600 | ~900 |
| 业务专有名词 | 12 处（传图定制、创意定制、定制页、定制模版页、造好物…） | **0** |
| 内联 JSON 示例 | 一大坨 36 行带真实业务值 | 0，指向契约 |
| 「禁止/不要」 | 11 处 | 2 处（且都是格式约束，不是业务判断） |
| 「输出前自检」 | 有 | 删（改由代码校验，见 v1 P1-1/P1-2） |
| 枚举值 | 散文里写死 `app/web/e2e` | 由契约按应用生成 |

**这份 prompt 换任何应用都不用改一个字。**

---

## 五、组装器

```python
# server/services/ai/prompting/assembler.py
@dataclass
class Slot:
    id: str
    title: str          # 渲染成 【标题】
    priority: int       # 预算不够时从低到高裁
    budget: int         # token 上限
    stable: bool        # True = 不随分片变化，排在前面以命中前缀缓存

class PromptAssembler:
    def build(self, *, job: str, profile: AppProfile, req: dict,
              tags: TagResult, scope: dict | None = None) -> BuiltPrompt:
        slots = SLOTS_BY_JOB[job]
        filled = [s for s in (self._fill(s, ...) for s in slots) if s.text]
        filled = self._fit_budget(filled, total=self.total_budget(job))

        stable   = [s for s in filled if s.stable]      # 画像/术语/库/契约
        volatile = [s for s in filled if not s.stable]  # 需求/反馈/分片范围

        return BuiltPrompt(
            messages=[
                {"role": "system", "content": self.methodology(job)},
                {"role": "user",   "content": render(stable)},     # 逐字节稳定 → 命中缓存
                {"role": "user",   "content": render(volatile)},
            ],
            response_schema=contract_for(job, profile),
            fingerprint=self.fingerprint(job, profile),   # 归因用
            slot_usage={s.id: s.tokens for s in filled},  # 可观测
        )
```

三个必须做到的性质：

1. **稳定/易变分离** —— 稳定块逐字节一致才能命中 provider 前缀缓存。这直接解决 v1 R6 里「前缀缓存完全没命中」。分片生成时（v1 P1-1/P1-2 的并发分片），稳定块在几十次调用间完全一样，收益被放大几十倍。
2. **预算裁剪有序** —— 超预算时按 `priority` 从低到高裁，且**必须记录裁了什么**。`requirement`（需求原文）优先级最高，永不裁；`few_shot` 最先裁。
3. **可归因指纹** —— `fingerprint = hash(methodology_version, library_version, profile_version, contract_version)`。落进 `dispatch_log`。质量变差时能立刻回答「是谁改了什么」。

---

## 六、版本与归因

现在 prompt 是代码常量，改动无迹可查。v2 给三层各自独立版本：

| 层 | 版本载体 | 谁改 | 变更影响 |
|---|---|---|---|
| 方法论 | 文件 + git（`methodology/*.md` 带 `version:` frontmatter） | 研发 | 全应用 |
| 知识库 | `library/*.yaml` 的 `version` + git | 研发/测试专家 | 全应用 |
| 应用覆写 | `qa_profile.library_overlay.version` | 该应用负责人 | 单应用 |
| 应用画像 | `qa_profile.version` | 该应用负责人 | 单应用 |

三者都进 `dispatch_log`（`dispatch_log.py:record_llm`），设置页每次调用可见。加上 v1 P3 的评测集，就形成闭环：

```
改一层 → 跑评测（≥2 个不同应用的 golden set）→ 指标不退化才合并 → 指纹留档
                                          ↓ 退化
                                  按指纹回滚到上一版
```

**评测集里必须至少有两个业务形态差异大的应用**（例如：造好物这类 App+运营平台电商，加一个纯后台 SaaS 或小程序）。否则"通用化"这件事本身没有验证手段 —— 这正是用户担心的"换应用效果就不好"的唯一有效防线。

---

## 七、和 v1 的衔接

| v1 条目 | v2 后的状态 |
|---|---|
| P0-1 截断抢救 / 动态 max_tokens | **不变**，仍然最先做 |
| P0-2 重试 + JSON 模式 | **升级**：`response_format` 从 `json_object` 升级成 `json_schema`（用 3.2 生成的 schema） |
| P0-3 桩用例显式化 `origin`/`failures` | **不变** |
| P0-4 token 瘦身 | **被组装器接管**：图谱裁剪、前缀缓存、去重都变成 assembler 的职责，不再散落在 `qa_role_jobs.py` |
| P1-1 脑图三段式 | **不变**，但每段的 prompt 由 assembler 出，校验规则由 L2 知识库驱动 |
| P1-2 用例 aspect 矩阵 | **升级**：`required_aspects` 从关键词 if 改成 `capability_tags` 查 L2 表 |
| P1-3 每个点加 `evidence` | **不变**，且在 3.2 的 schema 里变成 `required` |
| P1-4 并发工具 | **不变**，且因稳定块缓存命中，并发的成本收益更好 |
| P2-1 异步 + 进度 | **不变** |
| P2-2 定点重试 | **不变** |
| **P2-3 prompt 瘦身** | **被本文档整节取代** |
| P2-4 双模型分工 | **不变**，能力打标（3.3）正好是便宜模型的活 |
| P3 评测集 | **加强**：必须覆盖 ≥2 个业务形态不同的应用 |

---

## 八、落地顺序

前提：v1 的 **P3 评测集** 和 **P0-1/2/3** 先做完。没有评测集就无法证明通用化没有让效果变差 —— 而这正是本次改造最大的风险。

| 步 | 内容 | 工作量 | 产出可验证点 |
|---|---|---|---|
| 1 | 抽 L1：把三个 prompt 拆成 `methodology/*.md` + `contracts/*.py`，业务事实**原样**搬进造好物的 `qa_profile`（先不动任何规则） | 2 天 | 评测集指标与改造前**持平**（这一步是纯搬家，不该有变化，持平就说明搬对了） |
| 2 | 建 assembler + slots，接管现有三个 job 的 prompt 拼装 | 2 天 | token 下降（前缀缓存命中 + 图谱裁剪）；指标持平 |
| 3 | 建 L2 知识库（`page_archetypes` / `exception_playbook` / `risk_dimensions`），把原 prompt 里的规则搬成 YAML | 2 天 | 异常覆盖率、原型必测维度覆盖率可自动算 |
| 4 | 能力打标 + 注入过滤 | 1.5 天 | 单次 prompt token 再降；指标持平或上升 |
| 5 | schema 化契约 + `response_format: json_schema` + jsonschema 校验 + 定点重问 | 1.5 天 | 格式错误率 → 接近 0 |
| 6 | 接入**第二个应用**跑通评测集 | 2 天 | **通用性验收**：第二个应用不改一行 prompt，指标达标 |
| 7 | 前端：应用画像编辑页 + 插口用量可视化 | 2.5 天 | 非研发能自己维护画像 |
| 8 | 版本指纹 + 评测归因看板 | 1 天 | 每次调用可回答"用的哪版" |

**第 1 步是纯搬家、指标应当持平**，这是整个改造最重要的安全检查点 —— 如果搬完就变差，说明有隐性依赖没搬到，先修再往下走。

---

## 九、文件清单

**后端 `MiniOrangeServer`（新增）**

```
server/services/ai/prompting/
  assembler.py            # PromptAssembler
  slots.py                # 插口定义、优先级、预算、稳定性
  contracts.py            # 按 profile 生成 JSON Schema
  tagging.py              # 能力打标
  render.py               # 【标题】块渲染 + token 计量
  methodology/
    req_analyst.md        # 通用方法论，零业务
    mindmap_writer.md
    case_writer.md
  library/
    page_archetypes.yaml
    exception_playbook.yaml
    risk_dimensions.yaml
    test_design_rules.yaml
server/services/ai/app_profile/
  loader.py               # 读画像 + 合并 library overlay + 校验
  schema.py               # AppProfile dataclass
scripts/
  verify_prompt_slots.py  # 校验：方法论文件不含业务词、契约与 schema 一致、库 YAML 合法
```

**后端（改动）**

| 文件 | 改动 |
|---|---|
| `server/services/ai/roles_catalog.py` | 三个大 prompt 常量删除，改为 assembler 出；`_role()` 的 `system_prompt` 改成"方法论 + 指纹"，设置页展示的观察沙盒也走 assembler |
| `server/services/qa_role_jobs.py` | `analyze_req` / `draft_mindmap` / `draft_cases` 里手拼 `json.dumps({...})` 的部分全部换成 `assembler.build(...)` |
| `server/services/ai/regression/llm_client.py` | 支持 `response_schema` → `response_format: {type: json_schema}` |
| `server/services/ai/app_atlas.py` | `compact_atlas` 改成 `scope_atlas(atlas, paths, depth)` 供 `atlas_scope` 插口用 |
| `server/services/ai/dispatch_log.py` | 记录 `fingerprint` + `slot_usage` |
| `server/services/app_automation_service.py` | `automation.qa_profile` 读写与默认值 |
| `server/routers/rAppAutomation.py` | 应用画像 CRUD 接口 |

**前端 `MiniOrange`（新增/改动）**

| 文件 | 改动 |
|---|---|
| `src/views/Testing/AppProfilePanel.vue` | **新增**：端与别名、术语表（含 archetype 下拉）、库覆写、few-shot 管理 |
| `src/views/Testing/QaProcessPanel.vue` | 生成记录里展示本次指纹 + 插口用量；术语表缺词时提示"要不要补进画像" |
| `src/utils/dispatchLog.js` | 展示 `slot_usage` 占比条、被裁掉的插口 |
| `src/api/appAutomation.js` | 画像接口 |

---

## 十、风险与对策

| 风险 | 对策 |
|---|---|
| 通用化后效果反而变差 | 第 1 步是纯搬家、要求指标持平；每步都过评测集；指纹可回滚 |
| 画像没人维护，新应用是空的 | 内置库给足默认；画像可从图谱 + 已有用例自动初始化一版草稿让人改；术语表缺词时前端主动提示补 |
| 插口太多导致 prompt 又变重 | 每插口硬预算 + 命中过滤 + 裁剪日志；`verify_prompt_slots.py` 里加总预算上限断言，超了 CI 就红 |
| YAML 知识库变成第二个"越写越长" | 库长不等于 prompt 长（只注入命中项）；但仍要求每条 `must_cover` 必须能被确定性校验，写不出校验的不许进库 |
| 方法论文件被偷偷塞业务词 | `verify_prompt_slots.py` 扫 `methodology/*.md`，命中应用术语表里的词就报错 |
| L2 内置库和应用覆写冲突 | 覆写只支持 `add` / `disable`，不支持整体替换；`disable` 必须写理由 |

---

## 十一、一句话总结

把现在那三个"方法论 + 测试设计知识 + 造好物业务事实 + JSON 示例"糅在一起的大字符串，拆成**一份不含任何业务词的方法论 prompt**、**一份可增长但按需注入的测试设计知识库**、**一份每应用自己维护的画像**，由组装器在调用时按 job 拼装。换应用只改数据，接新场景只加 YAML，prompt 本体不再增长；而原来靠"祈求模型记住"的规则，全部落成代码校验。

---

# 附录 A：全仓通用化审计

对两个仓库做了一遍扫描。**role prompt 不是最严重的地方** —— 同类问题在执行链里更深，而且那里的后果不是「效果变差」，是**换应用直接功能失效**。

按严重度分三类。

---

## A 类：应用业务事实硬编码进执行逻辑 ★★★

这些是 Python 控制流，不是 prompt。造好物的 tab 文案直接决定了「已登录 / 在首页 / 在协议页」的判定结果。**换一个应用，这些判定会静默返回错误答案。**

| 位置 | 内容 | 换应用的后果 |
|---|---|---|
| `case_precondition_service.py:238-241` | `_main_tab_bar_logged_in`：`("首页","造物秀","消息","我的")` 命中 ≥3 才算已登录 | **永远返回 False** → 「已登录」前置检查永远失败 → 用例大面积阻塞 |
| `case_precondition_service.py:291-292` | 兜底判定同样枚举这四个 tab | 同上，且错误信息会写「底栏主导航齐全（首页/造物秀/消息/我的）」 |
| `page_context_service.py:526-538` | 首页识别：`home_tabs = ("首页","消息","我的","想要","造物秀","AI创意","想要成真")` 命中 ≥2 | 别的应用**识别不出首页** → 导航、断言、恢复全链路失准 |
| `page_context_service.py:509` | 协议页识别靠字符串 `"造好物 - 平台"` | 别的应用协议页漏判 |
| `page_context_service.py:683-686` | 断言特判：`if "造物秀" in expected and "造物秀" in screen_text` | 一条业务专属的 if 长在通用断言路径上 |
| `page_navigation_service.py:24` | `SEGMENT_TAB_LABELS = ["造物秀","AI创意","想要成真"]` | 顶栏分段 tab 导航对别的应用完全无效 |
| `page_navigation_service.py:698` | `if "平台用户协议" in blob or "造好物 - 平台" in blob or "造好物- 平台" in blob` | 同上（注意这里还手写了两种空格写法来兼容 OCR，正是"靠补字兼容"的典型） |
| `copilot_service.py:169-171` | `_SEGMENT_TAB_NAMES = {"造物秀","AI创意","想要成真","真造物秀","怪兽","艺术家专区"}` | Copilot 的 tab 识别只服务这一个应用 |
| `expectation_semantic_service.py:141-143` | 语义断言里特判造物秀 | 通用语义服务被单应用污染 |
| `figma_logic_service.py:29` | `TAB_LABELS = ["首页","消息","我的","想要","造物秀"]` | Figma 页面归类只对造好物成立 |
| `app_automation_service.py:568-580` | 变量名叫 **`generic_markers`**，内容是 `("造物者","造好物","造物者，你好", …)` | **命名为 generic、实为专用** —— 这行是整个问题的最佳标本 |
| `clip_query_plan.py:47-153` | `ZAOHAOWU_LOGIN_CHAIN` 整个查询表写死在 `.py` 里 | 代码注释已自认「一期 curated；二期 resolver 直接消费」—— 二期就是本次要做的事 |
| `clip_locate_service.py:161` | `for tab in ("造物秀","AI创意","想要成真")` | 同 A-6 |
| `page_profiles.yaml:355-366` | **全局** `home` profile 的 `label_patterns` / `screen_text_patterns` 里混进了 `造物秀`、`AI\s*创意` | 见下方专门说明 |
| `page_profiles.yaml:206` | `description: 输入手机号 + 获取验证码；造好物等常见子页` | 文档字段泄漏（轻） |

### A-14 `page_profiles.yaml` 值得单独说

这个文件是**全局单例、无应用维度**。加载逻辑（`page_profiles.py:82,164-197`）只支持用 `LOCATE_PROFILES_PATH` **整体替换**，没有 per-app scope，没有 overlay。

后果：支持第二个应用的唯一办法是**往全局文件里继续追加 patterns** —— 这和 prompt 越写越重是**同一个病，只是换成了 YAML**。而且全局 `home` profile 现在依赖 `造物秀 / 关注 / 推荐流 / AI创意` 作为 `screen_text_patterns`，一个 SaaS 后台或工具类应用的首页根本匹配不上。

**讽刺的是正确做法就在同一个目录里**：`app_packages.yaml:539-548` 是按 `key: zaohaowu` 分应用的数据文件。这个团队已经会这个模式了，只是 `page_profiles.yaml` 没用上。

### A 类的修法（和正文同一套三层）

```
resources/locate/page_profiles.yaml          # 通用原型：login / verify_code / form / list / detail / consent …
                                             # 只留跨应用成立的 patterns，清空所有业务词
App.automation.ui_profile                    # 每应用覆写（新增插口，与 qa_profile 并列）
  surfaces_nav:
    bottom_tabs:  [首页, 造物秀, 消息, 我的]      # 从代码搬到这里
    segment_tabs: [造物秀, AI创意, 想要成真]
  login_signals:
    logged_in_when: {bottom_tabs_hits: 3}     # 阈值也变成数据
    login_page_markers: [一键登录, 访客浏览, 验证码登录]
  legal_markers: ["造好物 - 平台", 平台用户协议]
  home_markers:  [推荐, 关注]
```

配套的代码改造原则（和你在 iOS 驱动上已经定过的「多方案可插拔、禁止 if-backend 分支」是同一条）：

- `_main_tab_bar_logged_in(blob)` → `_main_tab_bar_logged_in(blob, ui_profile)`
- 通用服务里**不允许出现任何应用专属字符串**；`verify_prompt_slots.py` 扩成 `verify_no_app_literals.py`，扫 `server/services/shared/**`、`server/services/local/**`、`page_profiles.yaml`，命中任一应用的 `lexicon` / `aliases` 词表就 CI 红。
- `clip_query_plan.py` 的 `ZAOHAOWU_LOGIN_CHAIN` 搬进 `ui_profile.clip_plans`（完成它注释里说的"二期"）。

---

## B 类：领域枚举写死，且 py / js 各写一份 ★★☆

不是应用专属，但**业务形态一变就不够用**，而且同一份规则在前后端各存一份、已经开始不一致。

### B-1 `platform` 枚举

写死成 `app / web / e2e`，散落四处：

| 位置 | 形式 |
|---|---|
| `roles_catalog.py:180,253,267-268` | prompt 散文 + JSON 示例 |
| `qa_role_jobs.py:705` | `plat_label = {"app":"App","web":"Web","e2e":"端到端"}` |
| `qa_role_jobs.py:249` | user note 里又写一遍「platforms 用 app/web/e2e」 |
| `qaProcess.js:122-125` | 前端正则再判一遍 |

没有小程序、桌面端、开放 API、IoT、SDK。**正文 3.2 的 schema 按 `profile.surfaces` 生成，就是为了修这个** —— 但 `plat_label` 和 `qaProcess.js` 这两处也必须一起改成读画像，否则 schema 放行了、渲染层又把它丢了。

### B-2 `"双端"` 这个魔法字符串

`qa_role_jobs.py:385,662`、`cover_import.py:266`、`feishu_regression_service.py:1918` 把 `"双端"` 当默认值。它既不在 `app/web/e2e` 里，也没在任何枚举里声明 —— 是个**隐式的第四取值**，而 `feishu_regression_service.py:1918` 还把它和 `"all"` 等价处理。这类"未声明的默认值"在换应用时最容易出错，应该在画像里显式声明成 `default_platform` 或直接取消。

### B-3 `aspect` 枚举 正向/异常/边界/权限

| 位置 | 形式 |
|---|---|
| `qa_role_jobs.py:665` | `{"正向":"ok","异常":"ex","边界":"bd"}` |
| `qa_role_jobs.py:681-686` | 关键词 → aspect 的映射（上传/保存/下单/提交 → 异常；输入/数量/空/格式/大小 → 边界） |
| `qaProcess.js:137-154` | 前端**又写了一套**，且关键词表和后端不一致（前端用 `异常|失败|取消|超时|无网|错误`、`边界|空|上限|重复`） |
| `qaWorkflow.js:29,300` | UI 文案里再写一遍 |

正文 3.3 的 `capability_tags` + L2 `test_design_rules` 就是这个的正解，但要补一条：**前后端必须共用同一份定义**。建议后端把 L2 库通过接口下发（`GET /ai/test-design-library`），前端不再自己维护关键词表 —— 否则永远会漂移。

### B-4 `_looks_web` / `_looks_app`

`qa_role_jobs.py:216-221` 的正则含 `运营平台|运营后台|cms`，`qaProcess.js:123` 是同一份正则的副本。这本质上就是画像里的 `surfaces[].aliases`（正文 3.5），应该由数据驱动、由后端唯一提供。

---

## C 类：UI 文案与示例硬编码 ★☆☆

后果最轻（不影响功能），但用户第一眼就会看到别的应用的业务名：

| 位置 | 内容 |
|---|---|
| `RolesPage.vue:12-16` | 角色示例提问：「传图定制和创意定制怎么拆开？」「从我的进定制模版再写步骤」「按「我要发造物秀」筛一个测试账号」 |
| `AssetsPage.vue:164,262` | 「写一句场景，例如「我要发造物秀」」 |
| `AssetsPage.vue:343` | 「例如：2024注册、已领取新人礼、造物秀白名单」 |
| `QaProcessPanel.vue:1068-1069` | 重试脑图/用例的 placeholder，整段是造好物业务 |
| `QaProcessPanel.vue:2155` | 需求补充说明 placeholder，整段是定制页需求 |
| `plugins/capabilities/launch_app.yaml:39` | `examples: ["打开 造物相机", …]` |
| `plugins/capabilities/assert_visual.yaml:26` | `"底部 tab 出现 首页/造物秀/消息/我的"` |

**修法**：placeholder / 示例提问从画像的 `lexicon` + `few_shot` 动态生成。画像为空时给中性文案（「例如：我要发一条动态」）。这样新接入的应用不会看到别人的业务名。

---

## 反面样板：做对了的地方

值得指出，因为它们证明这套做法在本仓已经可行：

| 位置 | 为什么是对的 |
|---|---|
| `server/services/ai/regression/prompts.py`（1069 行） | **几乎零业务词**。用 `com.example.app`、`com.miniorange.app` 占位，页面用「进入首页并出现底部 tab」这类原型描述。**执行链的 prompt 反而比 QA 角色的 prompt 干净得多** —— 这份文件应该作为 L1 方法论的写法基准 |
| `server/resources/locate/app_packages.yaml` | 按 `key` 分应用的数据文件，正是 `page_profiles.yaml` 缺的结构 |
| iOS 驱动的多方案可插拔（禁止 `if backend ==` 分支） | 同一条原则，只是还没推广到 QA / 定位 / 导航链路 |

---

## 修正后的优先级

审计结果改变了正文第八节的顺序 —— **A 类比 prompt 重构更急**，因为它是功能性失效而不是质量下降，而且它会让「接入第二个应用」这个通用性验收点（正文第八节第 6 步）**根本跑不起来**：用例会在前置检查阶段就全部阻塞，永远走不到写用例那一步。

| 优先级 | 内容 | 理由 |
|---|---|---|
| **P-1** | v1 的 P3 评测集 + P0 截断/重试/桩用例 | 不变，仍是前提 |
| **P0（提前）** | **A 类：`ui_profile` 插口 + 通用服务去业务字面量** | 换应用会静默功能失效；且是第二应用验收的前置 |
| P0 | `verify_no_app_literals.py` 进 CI | 防回流。没有这个，清完还会长回来 |
| P1 | 正文 1~5 步：方法论/知识库/画像/组装器/schema | 原计划 |
| P1 | B 类：枚举收口到画像 + L2 库接口下发前端 | 与正文 3.2/3.3 同批做，顺带修 py/js 双份漂移 |
| P2 | 正文第 6 步：接入第二个应用做通用性验收 | 依赖 A 类完成 |
| P3 | C 类：UI 文案动态化 | 体验问题，最后做 |

---

## 一句话

QA 角色的 prompt 只是**症状最显眼**的地方。真正危险的是 `case_precondition_service` / `page_context_service` / `page_navigation_service` 这些**名字通用、实现专用**的服务 —— 那里换应用不是"写得不好"，是"判定错了还告诉你判定对了"。`app_automation_service.py:569` 那个叫 `generic_markers` 却装着"造物者、造好物"的常量，是这一整类问题最好的注脚。
