# 脑图 ⇄ 应用图谱：双向关联与对齐沉淀方案

> 目标：支持从外部导入脑图，反推应用图谱；并让每一次人工确认都沉淀成下一次的对齐先验。
>
> 依赖既有设计：`docs/plan-qa-role-quality-v2.md` 的三层分层（内置默认 / YAML 种子 / 运行期覆写）。
> 本文档新增的是**第四层：运行期学习**，不改动前三层的职责划分。

---

## 一、问题重述

### 1.1 关联是单向的

图谱 → 脑图这个方向已经通了：

```458:471:server/services/qa_role_jobs.py
def draft_mindmap(req: dict, cases: list | None = None, atlas_doc: dict | None = None, *, user_note: str = "") -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    intent = req.get("atlas_intent") if isinstance(req.get("atlas_intent"), dict) else {}
    hang = intent.get("hang") if isinstance(intent.get("hang"), dict) else {}
    atlas_paths = atlas.paths_for_req(atlas_doc, req.get("id") or "") or hang.get("paths") or []
```

`compact_atlas` 当 stable 前缀命中缓存，`paths_for_req` 当挂载锚点。

反方向没有。`propose_atlas` 的输入是 `current_atlas` + `requirements` + `existing_cases` + `draft_cases` + `module_hints`（`qa_role_jobs.py:1540-1579`），**脑图不在其中**。脑图是这个系统里信息密度最高的产物（一条需求几百个测试点，天然带模块层级），却完全没有反哺图谱。

### 1.2 导入脑图的层级判定是硬编码启发式

```43:49:server/services/cover_import.py
def _kind_for(text: str, depth: int, has_kids: bool) -> str:
    low = str(text or "").strip().lower()
    if depth <= 1 and any(k in low for k in ("app", "web", "ios", "android", "端到端", "e2e", "运营")):
        return "platform"
    if not has_kids:
        return "point"
    return "module" if depth <= 2 else "feature"
```

三个问题：

1. **含应用字面量**。`运营` 来自造好物的「运营平台」，属于 `scripts/verify_no_app_literals.py` 要拦的那类词。换应用就失准。
2. **只看 depth，不看图谱**。导进来的「定制页」和图谱里的「定制工具」是两个独立节点，谁也不认识谁。
3. **端的枚举也是写死的**。同样的问题在 `qa_role_jobs.py:348` 和 `_mindmap_platforms` 的 `add()` 里再出现一次：

```348:365:server/services/qa_role_jobs.py
MINDMAP_PLATFORM_LABELS = (("app", "App"), ("web", "Web"), ("e2e", "端到端"))
MINDMAP_TOKENS = 8192
MINDMAP_SHARD_TIMEOUT_SEC = 90


def _mindmap_platforms(req: dict) -> list[tuple[str, str]]:
    """按需求分析里的端拆脑图调用。整棵树一次吐 8192 token 必然截断。"""
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    found: set[str] = set()

    def add(raw: str) -> None:
        s = str(raw or "").strip().lower()
        if s in ("app", "android", "ios", "mobile", "客户端"):
            found.add("app")
        elif s in ("web", "ops", "admin", "cms", "h5", "运营", "后台", "运营平台"):
            found.add("web")
        elif s in ("e2e", "端到端"):
            found.add("e2e")
```

这正是 v2 方案里 `surfaces` 那一项要解决的事，本方案顺带落地。

### 1.3 没有沉淀

人在「用例 · 变更」里确认过一次「脑图里的 X 就是图谱里的 Y」，这个判断不会被记住。下一条需求、下一次导入，模型和规则都要重新猜一遍，还可能猜出不一样的结果。这是「学习能力」缺失的根因——不是模型不够聪明，是**人的判断没有落盘**。

### 1.4 不缺的东西

`server/services/ai/app_atlas.py` 已经很完整，反推不需要新造轮子：

| 能力 | 函数 |
|---|---|
| 建路径 / 挂需求 | `ensure_module` `ensure_feature` `ensure_path` `hang_req` |
| 查找 | `find_module` `find_feature` `flatten_tree` `paths_for_req` |
| 合并模型输出 | `merge_payload`（已有同名节点保留 id） |
| 无模型降级 | `rule_propose` |
| 差异 / 入队 / 审核 | `diff_atlas` `diff_lines` `enqueue_patch` `accept_patch` `reject_patch` `apply_reject_feedback` |

patch 人审链路（`AtlasChangeReview.vue`）也已经通了。**本方案只往这套东西上接。**

---

## 二、目标架构

```
┌────────────────────────────────────────────────────────────┐
│ 对齐层  atlas_align.py                                      │
│   输入：任意文本（脑图节点名 / 用例 module / 飞书分区名）      │
│   输出：{target_id, kind, path, score, how}                 │
│                                                             │
│   L1 归一化精确匹配   代码，跨应用通用，不含业务词             │
│   L2 术语表命中       UiProfile.lexicon（YAML 种子，人工维护） │
│   L3 别名表命中       m_atlas_alias（运行期学习，人审沉淀）★新 │
│   L4 模糊匹配         difflib + token 重叠 → 仅产出「建议」    │
│   L5 未命中           交给 LLM 或人                          │
└────────────────────────────────────────────────────────────┘
        ↑                          ↑                    ↑
   导入脑图时对齐            反推图谱时对齐         生成脑图时对齐
   cover_import         atlas_from_mindmap        draft_mindmap
        │                          │
        └──────────┬───────────────┘
                   ↓
        exact 命中 → 直接合并进图谱
        新增 / 模糊 → enqueue_patch 进人审队列
                   ↓
            人 accept → 别名表 hits+1、review_status=approved
            人 reject → 别名表 review_status=rejected，下次不再提
```

核心原则，三条：

**一、模糊命中只产出建议，绝不静默合并。** 一次误判会被后续所有导入继承，越用越歪。只有人确认过的对齐才进别名表。这条是整个「学习」是否成立的分水岭。

**二、测试点不进图谱。** 图谱是产品结构（模块 / 页面 / 功能），测试点是覆盖清单。几百个叶子节点灌进去图谱就废了。覆盖密度用 `feature.point_count` 表达。

**三、端（platform）也不进图谱。** `app_atlas` 的数据模型里**根本没有 platform 这一层**——`normalize_module` / `normalize_feature` 都不含该字段，端只存在于 `requirement.understanding.impact.platforms`、脑图第一层 `kind=platform` 节点、以及测试点的 `platform` 上。前端在算路径时已经明确跳过这一层：

```406:409:src/utils/appAtlas.js
  const walk = (n, prefix) => {
    const skip = n.kind === 'root' || n.kind === 'platform'
    const here = skip ? prefix : [...prefix, String(n.name || '').trim()].filter(Boolean)
    if (!skip && here.length) {
```

所以反推时必须**跳过 root 和 platform 两层**再开始 `ensure_path`，否则会在图谱顶层凭空造出「App」「Web」「端到端」三个模块，并且每个端下面复制一套同名子树——这恰好是 `REQ_ANALYST_IMPACT_PROMPT` 明令禁止的事（「不要为每个端复制一套同名模块」）。端的信息落在 patch 的 `reason` 和挂载关系上，不落在骨架里。

**四、只能有一套匹配器。** 详见第五节——前端已经存在一套用于可视化的匹配逻辑，本方案必须收敛而不是叠加。

**五、代码里不留业务字面量。** 通用信号（去后缀、全半角、括号）在代码；应用专属词（端别名、业务术语）在 `UiProfile`；学出来的对齐在别名表。和 v2 的分层一致，`verify_no_app_literals.py` 继续守。

---

## 三、数据结构

### 3.1 新表 `m_atlas_alias`

新建 `server/models/atlas_alias.py`，在 `main.py` 里 `import ... # noqa: F401` 注册（同 `case_baseline` 的做法）。SQLite + `Base.metadata.create_all`，新表自动建，无需迁移脚本。

```python
class MAtlasAlias(Base):
    __tablename__ = "m_atlas_alias"
    __table_args__ = (UniqueConstraint("app_id", "alias_norm", name="uq_atlas_alias_app_norm"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_id = Column(String(64), index=True, default="")
    alias = Column(String(128), nullable=False)          # 原样文本：脑图里怎么写的
    alias_norm = Column(String(128), index=True, default="")  # 归一化后的 key
    target_id = Column(String(64), index=True, default="")    # mod-xxx / feat-xxx
    target_kind = Column(String(16), default="module")        # module | feature
    target_path = Column(JSON, default=list)                  # ["社区","帖子详情页","点赞"] 快照，便于 id 失效时兜底
    source = Column(String(16), default="import")             # import | llm | human | case
    review_status = Column(String(16), default="pending", index=True)  # pending | approved | rejected
    hits = Column(Integer, default=0)                    # 命中次数，管理页排序用
    score = Column(Integer, default=0)                   # 首次建议时的相似度 ×100
    note = Column(String(512), default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
```

`review_status` / `source` 的取值刻意和既有知识库一致（`system_settings_service.upsert_knowledge_item` 的 pending/approved 语义），保持一套心智模型。只有 `approved` 的别名参与对齐；`rejected` 的进负样本，阻止重复建议。

存储层 `server/services/ai/atlas_alias_repo.py`，照 `regression/case_memory/repo.py` 的写法：只读写表、不掺业务规则、`session_scope()` 便利上下文。

### 3.2 脑图节点已有 `path`，先把它填上

**这一项优先级高于 `atlas_ref`。** 脑图节点本来就有 `path` 字段，语义就是图谱路径——LLM 生成的脑图会写（`MINDMAP_WRITER_SYSTEM_PROMPT` 的契约里 module/feature/point 都带 `path`），前端 `mindNode` 也会读（`appAtlas.js:477`），`placeBranch` 拿它做第一优先的定位依据。

但**导入的脑图完全没有 `path`**：`cover_import._node()` 只写 `id/text/kind/children`，加上可选的 `detail/platform/point_id`。这就是导入脑图在看板上「叠不上去、只能挂到根下」的直接原因，也是反推图谱最省力的切入点——光把 `path` 填对，前端叠加和后端 `ensure_path` 就都能工作了。

所以 P0 的第一件事不是造 `atlas_ref`，而是**在导入时按对齐结果回填 `path`**（跳过 root/platform 后的祖先链）。

### 3.3 脑图节点新增 `atlas_ref`

```json
{
  "id": "n-8f2a1c00",
  "text": "定制页加载",
  "kind": "feature",
  "atlas_ref": {
    "module_id": "mod-3f1a",
    "feature_id": "feat-9c2b",
    "how": "alias",
    "score": 100
  },
  "children": [...]
}
```

`how` ∈ `exact | lexicon | alias | fuzzy | llm | none`。`path` 是人类可读、跨 id 稳定的定位；`atlas_ref` 是 id 级绑定，用于图谱改名后还能找回来。两者都要，职责不同。

### 3.4 图谱 feature 新增 `point_count`

`normalize_feature` 加一个 `point_count: int`。

数据来源不是脑图树，而是 `understanding.points`——那才是覆盖的主数据。脑图叶子经 `_sync_points_from_mindmap`（`qa_role_jobs.py:792-804`）展平成 `understanding.points[]`，每项带 `path` / `platform` / `case_ids` / `waived`。统计 `point_count` 应该按 `points[].path` 归组，这样 `waived`（已豁免）的点可以排除，覆盖密度才有意义。

### 3.5 注意 `_clip_mindmap` 的截断

`apply_mindmap` 落库前会截断节点文案：测试点 ≤40 字，其他层级 ≤20 字。**别名的 key 必须按截断后的文本计算**，否则一个 25 字的模块名，导入时按原文存别名、下次匹配时拿截断后的 20 字去查，永远查不中。

### 3.6 patch 新增 `aliases`

`normalize_patch` 要同步加，否则会被过滤掉——它是白名单式的：

```133:154:server/services/ai/app_atlas.py
def normalize_patch(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip()
    if not pid:
        return None
    status = raw.get("status") if raw.get("status") in ("pending", "accepted", "rejected") else "pending"
```

`enqueue_patch` 的 fingerprint 也要把 aliases 纳进去，否则「只有别名建议不同」的两个 patch 会被去重掉。

---

## 四、实施分期

### P0：纯规则闭环（不碰模型，风险最低）

**已完成。** 实际落地与原计划有五处偏差，都记在下面。

| # | 文件 | 改动 |
|---|---|---|
| 1 | `server/services/ai/app_profile.py` | `GENERIC_SURFACES` 常量 + `UiProfile.surfaces` / `surface_extra`，方法 `surface_of(text, loose=)` / `surface_options()` / `declared_surfaces()` / `surface_label()` |
| 2 | `server/resources/app_profiles/zaohaowu.yaml` | 声明 `surfaces: [app, web]` |
| 3 | `server/services/qa_role_jobs.py` | 删掉 `MINDMAP_PLATFORM_LABELS`；`_mindmap_platforms` / `_extract_platform_branch` 改读画像 |
| 4 | `server/services/ai/atlas_align.py` | 新建。`norm_name` / `similarity` / `Aligner.match` / `match_path`，L1 精确 + L4 模糊，术语表作反向守卫 |
| 5 | `server/services/ai/app_atlas.py` | `normalize_module` / `normalize_feature` 加 `point_count`，`flatten_tree` / `compact_atlas` 带出来 |
| 6 | `server/services/ai/atlas_from_mindmap.py` | 新建。`infer()` 跳过 root/platform，结构节点落模块/功能，测试点只累加 `point_count`，同时回填 `path` / `atlas_ref` |
| 7 | `server/services/cover_import.py` | `Hints` + `_looks_like_point`；`_kind_for` 走画像和对齐层；`_import_mindmap` 按确定性分流合并 / `enqueue_patch` |
| 8 | `src/utils/appAtlas.js` | `findById` / `refIdOf`；`placeBranch` 和 `mergeChild` 都改成 id 优先 |
| 9 | `scripts/verify_atlas_align.py` + `scripts/verify_mindmap_to_atlas.py` | 新建 |

**偏差一：通用端名留在代码里，没搬进 YAML。** 原计划要把 `运营/运营平台/后台` 搬进 `zaohaowu.yaml`。做的时候发现这类词和「首页/我的」是同一层级——它们是产业通用词，不是某个应用的私有叫法。`admin`/`cms`/`h5`/`后台`/`运营平台` 换个应用照样成立，搬进画像等于每个应用都要重抄一遍，还会逼着把 `MINDMAP_WRITER_SYSTEM_PROMPT` 里那句「运营平台、后台、CMS 一律挂在 Web 下」也拆掉——而那句话是通用指令，拆了就是纯损失。所以它们进了 `app_profile.GENERIC_SURFACES`，和 `GENERIC_NAV_WORDS` 一样从 `all_literals()` 里排除；YAML 只声明**这个应用有哪几个端**，以及非标准端（小程序 / 桌面端）的名字和别名。

**偏差二：反推逻辑独立成模块，没塞进 `app_atlas.py`。** 它要用 `atlas_align`，而 `atlas_align` 要用 `app_atlas.flatten_tree` —— 放一起就是循环导入。顺带 `app_atlas.py` 已经 930 行，P1 的 LLM 版本也该落在同一个新模块里。

**偏差三：不用 `feishu_hints` 装拿不准的节点。** patch 队列本身就是「拿不准」的通道：新建节点会以 `+ 模块 X` 的形式出现在 diff 里等人点头，再多一条 hints 只是把同一件事说两遍，还得给一个飞书命名的字段塞脑图数据。拿不准的东西改成进 `Outcome.review`，它一非空就强制走 patch 路径。唯一真正的形状冲突——图谱把某个名字当叶子功能、脑图里它下面还有结构——也记在 `review` 里，子结构按测试点计数，不悄悄吃掉。

**偏差四：多了一条「像不像一句话」的判据。** `_looks_like_point`：超过 14 字或带句读的节点一律当测试点，哪怕它有子节点。人写脑图常把一个点再拆几个子情况（「超过 10MB 时提示图片过大，不允许提交」下面挂三条），没这条判据它会变成图谱上的一个功能。

**偏差五：前端要改的不止 `placeBranch`。** 子节点走的是 `mergeChild`，它只按名字找既有节点。只改 `placeBranch` 的话，顶层能对上，但「图谱里叫本地上传提交、脑图里叫图片上传」的同一个功能会在看板上并排出现两份。两个函数都得 id 优先。

**一个需要注意的互斥**：tick 里 `propose_atlas` 的触发条件是 `not atlas.pending_patches(patches) or force`（`qa_role_jobs.py:1825`）。导入产生的 pending patch 会让 tick 暂时不再提新的图谱建议，直到人处理完。这个行为是对的（避免两个来源打架），但前端要说清楚，否则用户会以为流程卡住了。

P0 结束后：导入一份 Markdown 脑图，能自动对上已有模块、把新模块作为 patch 推到「用例 · 变更」等人确认，并且脑图能正确叠加到看板上。

### P1：学习沉淀

| # | 文件 | 改动 |
|---|---|---|
| 1 | `server/models/atlas_alias.py` | 新建表 + `main.py` 注册 |
| 2 | `server/services/ai/atlas_alias_repo.py` | 新建存储层 |
| 3 | `server/services/ai/atlas_align.py` | 把别名表灌进已经留好的 `Aligner.aliases` / `aligner_for(aliases=)` 口子 |
| 4 | `server/services/ai/app_atlas.py` | `accept_patch` 通过时写别名（approved, hits+1）；`reject_patch` 时写 rejected |
| 5 | `server/services/ai/roles_catalog.py` | 新增 `MINDMAP_TO_ATLAS_PROMPT` |
| 6 | `server/services/qa_role_jobs.py` | 新增 job `atlas_from_mindmap`，走 `_ask_json` + `merge_payload` + `enqueue_patch`；加进 `LLM_JOBS` |
| 7 | `scripts/verify_atlas_alias_learning.py` | 新建。确认 → 命中 → hits 累加；驳回 → 不再建议 |

`atlas_from_mindmap` 的 prompt 输入必须控 token：只喂 `kind != point` 的结构骨架，每个模块带 `point_count` 和前 3 个点名。一份四百多测试点的脑图全量塞进去会直接超 `MINDMAP_TOKENS`。输出沿用 `REQ_ANALYST_IMPACT_PROMPT` 的 `modules` / `hang` 结构（这样能直接喂 `merge_payload`），额外要一段 `aliases`。

### P2：反向同步与可视化

| # | 文件 | 改动 |
|---|---|---|
| 1 | `server/services/ai/app_atlas.py` | `relink_mindmap(req, atlas)`：按 `atlas_ref` 同步改名、标 `orphan` |
| 2 | `server/routers/rAppAutomation.py` | patch accept 后对受影响需求跑 `relink_mindmap` |
| 3 | ~~`src/views/Testing/CoverImportDialog.vue`~~ | **已在 P0 做掉**。这条不是锦上添花：不把待确认说出来，人不知道还要去点头，图谱一直停在旧骨架上，下次导入又叠一条同样的建议，而且 tick 在 patch 处理完前不会再提新图谱建议——看着就像流程卡死了。导入回执 + 两个父组件自动切到「图谱变更」 |
| 4 | `src/views/Testing/AtlasChangeReview.vue` | 展示别名建议段：「脑图里叫 X，图谱里是 Y，确认后以后自动对齐」 |
| 5 | `src/views/Testing/QaProcessPanel.vue` | orphan 节点加标记 |
| 6 | `src/views/Settings/` | 别名管理页（按 hits 排序，可改可删） |

---

## 五、必须收敛的既有匹配器

调研发现前端 `src/utils/appAtlas.js` 里**已经有一套完整的名称匹配逻辑**，为看板叠加服务：

| 函数 | 行号 | 作用 | 对应本方案的层 |
|---|---|---|---|
| `walkPath(node, parts)` | 309-318 | 按路径逐级下钻 | 路径定位 |
| `findDeep(node, name)` | 284-292 | 全树精确名匹配（`normName` 归一化后） | L1 精确 |
| `findLoose(node, name)` | 294-307 | **子串包含**模糊匹配，带 `length < 4` 守卫，取最长命中 | L4 模糊 |
| `namesMatch(a, b)` | 278-282 | `name`/`full` 交叉比对 | L1 精确 |
| `placeBranch(root, branch)` | 425-459 | 上面几个的编排：path → 精确 → 模糊 → 斜杠拆分 → 兜底挂根 | 整个对齐层 |
| `assignCasesToAtlas(...)` | 137+ | 用例 → 图谱 feature 的归属推断 | 同类问题的另一个实例 |

这是个真实的风险：如果后端新增对齐层而前端继续用 `findLoose` 自己猜，**看板上显示的归属会和实际合并进图谱的归属不一致**，而且用户无法干预——他在「图谱变更」里确认的是后端算的那一版，看板画的是前端算的另一版。别名表越攒越多，两边会越走越远。

收敛方式：

1. **后端权威。** 对齐结果落到 `path` + `atlas_ref` 上并持久化。
2. **前端降级为消费者。** `placeBranch` 优先信 `atlas_ref.module_id` / `atlas_ref.feature_id`，其次信 `path`；`findDeep` / `findLoose` 只在两者都缺失时兜底（老数据、未走过导入的脑图）。
3. **`findLoose` 不再产生新的对齐事实**，只影响渲染落点。任何要写进图谱的对齐一律来自后端。
4. `assignCasesToAtlas` 同理，P2 阶段一并接到后端对齐层（用例的 `module` 字段本来就是对齐层的输入之一）。

这条收敛不做，别名表的价值会被前端的自作主张抵消掉，所以它属于 P0 的验收范围而不是 P2。

---

## 六、与既有方案的关系

- **v2 的 L3 应用层**：本方案给 `UiProfile` 补 `surfaces`，并把 `lexicon` 真正用起来（现在只有 `archetype_of` 一个消费点）。别名表是 L3 之上的第四层，区别是 **L3 人工维护、第四层自动学习 + 人审**。
- **知识库（`knowledge_capture_service` / `knowledge_review_service`）**：那套沉淀的是「执行时的界面事实」，粒度是操作路径；别名表沉淀的是「命名对齐」，粒度是节点名。两者不合并，但 `review_status` / `source` 的取值保持一致。
- **`verify_no_app_literals.py`**：`cover_import._kind_for` 和 `qa_role_jobs._mindmap_platforms` 里的关键词都清掉了，但因为通用端名按偏差一留在 `GENERIC_SURFACES`（从 `all_literals()` 排除），这个脚本的字面量总数没变，仍是 17 个。它守的是「别把应用私有词写回通用服务」，非标准端的名字进 YAML 后一样会被它盯住。

---

## 七、验收标准

1. 导入一份不带任何 kind 标注的 Markdown 大纲脑图，模块层级能对上现有图谱，不产生重名新分支。
2. 导入后图谱里**没有**测试点节点、**没有**「App」「Web」「端到端」模块，但 feature 上有 `point_count`（排除 `waived` 的点）。
3. 导入的脑图节点带上了 `path`，看板叠加时能落到正确的图谱分支，不再整棵挂到根下。
4. 看板显示的归属和实际合并进图谱的归属一致——同一个节点，`AtlasChangeReview` 里的 diff 行和 `AtlasBoardView` 里的落点指向同一条路径。
5. 同一个别名第二次导入走 `how="alias"`，不再进 patch 队列。
6. 驳回过的对齐建议不再重复出现。
7. 图谱模块改名后，脑图上对应节点的 `text` 跟着变；模块被删后节点标 `orphan`。
8. 换一个应用（无 YAML profile）跑导入，不会被造好物的业务词带偏——降级到纯 depth 推断，且不报错。
9. 一个 25 字以上的模块名，导入两次都能命中同一条别名（截断一致性）。
