# AI Plan Skills 与坐标化改造说明

> 状态：坐标化闭环**已落地**。本文档从原先的「改造提案」更新为「现状说明 + 残留项」。
> 最后核对：对照 `server/services/copilot_service.py`、`server/services/ai_plan_prompt.py`、
> `server/services/skills_registry.py`、`driver/tentacle/engine/mobile/mAdb.py`、
> 前端 `MiniOrange/src/views/Dialogue/index.vue`。行号为核对时的位置，仅供定位参考。

## 结论

AI（大模型）模式现在走的是**纯坐标化闭环**，不再依赖本地 OCR / CLIP / Hierarchy / LocateArbitrator：

1. 前端选择大模型引擎，只下发 `planningMode=ai` + `providerId`（`Dialogue/index.vue:100-102,531`），不参与截图。
2. **Server 端**在请求大模型前截取当前设备画面，压缩成低清晰度 JPEG base64，并附带原始屏幕宽高与预览图宽高（`_build_ai_screen_context`，`copilot_service.py:3243`）。
3. 大模型基于截图直接输出 **preview 像素坐标**的 `click/input/swipe`。
4. Server 用 `_scale_ai_plan_coordinates`（`3292`）把 preview 坐标映射回设备分辨率。
5. `_normalize_ai_step(require_visual_coordinates=True)`（`3048`）强制校验视觉动作必须带坐标，缺坐标直接丢弃并返回 `ai_error_info`。
6. 通过校验的视觉步骤被打上 `ai_coordinate_only=true`。
7. `execute_steps`（`4252`）看到该标记后只做坐标注入，不进入 `_resolve_click_target`。
8. 引擎层 `engine.click(..., skip_label_lookup=True, locate_method="ai_coordinate")`（`mAdb.py:1072`）直接坐标点击，不走 `click_by_label`。

链路概览：

```text
前端 planningMode=ai
  → Server 截图 _build_ai_screen_context（width/height + preview_width/preview_height + base64）
  → 视觉模型输出 preview 像素坐标
  → _scale_ai_plan_coordinates 映射到设备分辨率
  → _normalize_ai_step(require_visual_coordinates=True) 校验 + 打 ai_coordinate_only
  → execute_steps 坐标注入分支
  → engine.click(position=(x,y), skip_label_lookup=True, locate_method="ai_coordinate")
```

旧版协议允许的 `label` 型点击（只给 `label` 不给坐标）在 AI 模式下**会被拒绝**，不再触发本地定位兜底。

## 目标边界

### AI 负责

- 看当前屏幕截图。
- 判断用户意图对应的视觉目标。
- 输出 preview 像素坐标 `x/y`（Server 负责映射到设备坐标）。
- 可选输出 `bbox`、`label`、`confidence`、`reason` 用于审计。
- 判断无法安全定位时，返回 blocker，不允许让执行器兜底定位。

### 执行器负责

- 提供当前截图、原始屏幕尺寸与预览图尺寸给 AI。
- 把 preview 坐标映射回设备坐标。
- 校验 AI 返回的坐标在屏幕范围内。
- 按坐标执行点击、滑动、输入、按键。
- 截图记录 before/after。
- 记录设备准备、唤醒、解锁等前置事件。

### 执行器不能在 AI 模式下负责

- 用 OCR 找 `label`。
- 用 CLIP 找图标。
- 用 Hierarchy/DOM 找控件。
- 用 LocateArbitrator 在多个候选中做判断。
- 在 AI 缺少坐标时 fallback 到本地定位。

## 历史背景：之前为什么拆不出来（均已解决）

### 1. Prompt 曾经要求使用本地语义定位 —— ✅ 已解决

旧版 prompt 写的是「优先使用语义定位/多通道点击」「click 必须填写 label」，会诱导大模型返回 `label`。

现状（`ai_plan_prompt.py:18-38`）：

- 第 4~9 条明确要求 click/input/swipe 必须基于截图直接返回 preview 像素坐标，`coords_explicit=true`。
- `label`/`direction`/`field_hint` 只能作为审计说明，不能让本地执行器再判断。
- 没有截图或无法判断坐标时返回 blocker。
- Local Plan 不使用这套大模型 prompt，仍保留本地规则能力。

### 2. AI 请求没有当前截图 —— ✅ 已解决

现状：`_build_ai_screen_context`（`3243`）在 AI plan 前截图 → 转 RGB → 最长边压到 768 → JPEG quality=68 → base64，返回：

```json
{
  "image_path": "/static/xxx.png",
  "width": 1200,
  "height": 2608,
  "preview_width": 353,
  "preview_height": 768,
  "mime_type": "image/jpeg",
  "base64": "...",
  "data_url": "data:image/jpeg;base64,...",
  "note": "坐标请基于 preview_width/preview_height 返回，Server 会自动映射到设备 width/height。"
}
```

OpenAI 系通过 `_append_openai_image`（`image_url.detail=high`）注入；压缩最长边 **512px**（与 OpenAI 视觉 low-detail 上限对齐，`preview_width/height` 必须等于实际上传 JPEG 像素）。Anthropic 系通过 base64 image block 注入。

### 3. preview 坐标 → 设备坐标映射 —— ✅ 新增（原文档缺失）

`_scale_ai_plan_coordinates`（`3292`）按 `sx=width/preview_width`、`sy=height/preview_height` 缩放：

- click/input 缩放 `x/y`，swipe 缩放 `start_x/start_y` 与 `end_x/end_y`。
- 坐标超出 preview 范围（`> preview+8`）时视为模型已直接给设备坐标，跳过二次放大。
- preview 与设备同尺寸时不缩放（`same_dimensions`）。
- 缩放明细记入 `ai_debug.coordinate_scale`。

### 4. `_normalize_ai_step` 兼容了 label 型视觉动作 —— ✅ 已解决

现状（`3048`）：`require_visual_coordinates=True` 时

- `kind=click`：`x<=0 or y<=0` 直接返回 None（`3094`）。
- `kind=input`：`x<=0 or y<=0` 直接返回 None（`3115`）。
- `kind=swipe`：起止坐标任一 `<=0` 直接返回 None（`3140`）。
- 通过的步骤打 `ai_coordinate_only=true`。
- `label` 仍保留，仅用于审计展示。

AI plan 主流程（`4004-4060`）会把"是视觉动作但 normalize 失败"的步骤收集为 `invalid_visual_steps`，整单返回 `auto_run=false` + `ai_error_info`，不执行。

### 5. `_run_mobile_click` 默认会解析 label —— ✅ 已解决

现状（`1836`）：新增 `ai_coordinate_only` 参数（`1847`）。当为真时（`1899-1949`）：

- 校验 `x>0 && y>0`，否则返回失败「AI 模式 click 必须返回有效 x/y」。
- 校验坐标在屏内（`x<=screen_w && y<=screen_h`），否则返回超界失败。
- 直接 `engine.click(None, position=(x,y), skip_label_lookup=True, locate_method="ai_coordinate")`。
- **完全不调用** `_resolve_click_target`，不进 OCR/CLIP/Hierarchy/LocateArbitrator。

引擎层（`mAdb.py:1098-1158`）在 `skip_label_lookup=True` 时只做坐标 tap，不进入 `click_by_label`（`1159`）。

## 当前 Skills 清单

> 命名说明：AI 坐标化能力是用「旧 kind + `ai_coordinate_only` 标志位」实现的，
> **没有**注册成独立的 `visual_click`/`visual_swipe`/`visual_input` skill 名。
> 下文目标协议描述的是 AI 模式下这些 kind 的实际字段。

### window_start / window_close / window_switch（启动 / 关闭 / 切换应用）

- 来源：`skills_registry.py` 的 `public/window`；触发 `Plan kind=open_app/close_app`。
- 是否参与本地判断：否。AI 坐标化后保留。
- 现状：`_normalize_ai_step`（`3158`）要求 `open_app/close_app` 必须解析出 `package`，并用 `app_packages.resolve_known_app_by_alias/by_package` 纠正包名；无包名返回 None。
- 要求：参数必须明确包名/Bundle/URL/应用名，不能靠本地猜测。

### gesture_click（点击 / 长按）

- 来源：`public/gesture`；触发 `Plan kind=click`；执行 `_run_mobile_click`。
- 现状：
  - AI 模式（`ai_coordinate_only=true`）必填 `x/y`，`coords_explicit=true`，执行器跳过 `_resolve_click_target`。
  - Local 模式仍走 `_resolve_click_target` 多通道定位。
- 是否参与本地判断：AI 模式否 / Local 模式是。

AI 模式目标协议：

```json
{
  "kind": "click",
  "x": 449,
  "y": 2492,
  "coords_explicit": true,
  "ai_coordinate_only": true,
  "label": "造物秀",
  "bbox": { "x": 410, "y": 2450, "w": 90, "h": 60 },
  "confidence": 0.86,
  "reason": "截图底部导航栏右侧显示「造物秀」入口"
}
```

### gesture_swipe（滑动）

- 来源：`public/gesture`；触发 `Plan kind=swipe`。
- 现状：
  - AI 模式走 `_run_mobile_swipe_coords`（`253`），必填 `start_x/start_y/end_x/end_y`，可选 `duration_ms`（默认 350）；坐标超界返回失败（`274`）。
  - Local 模式走 `_run_mobile_swipe`（`228`）方向语义 `up/down/left/right`，执行器内部生成坐标。
- `direction` 在 AI 模式只作审计展示。

AI 模式目标协议：

```json
{
  "kind": "swipe",
  "start_x": 600,
  "start_y": 1900,
  "end_x": 600,
  "end_y": 850,
  "duration_ms": 350,
  "ai_coordinate_only": true,
  "reason": "需要向上滑动列表查看更多内容"
}
```

### mobile_input（文本输入）

- 来源：移动引擎直连；触发 `Plan kind=input`。
- 现状：
  - AI 模式走 `_run_mobile_input_coords`（`483`）：先点击坐标，再输入文本；坐标超界返回失败（`507`）；点击用 `skip_label_lookup=True`（`522`）。
  - Local 模式走 `_run_mobile_input`（`302`），依赖 field hint、上一步 click 的 `focus_rect`、u2 EditText、焦点查找。
- 注意：`_normalize_ai_step` 在 AI 模式下仍透传 `field_hint`（`3120`），但 `_run_mobile_input_coords` 实际不使用它（见「残留项 2」）。

AI 模式目标协议：

```json
{
  "kind": "input",
  "x": 520,
  "y": 1180,
  "text": "13800138000",
  "coords_explicit": true,
  "ai_coordinate_only": true,
  "label": "手机号输入框",
  "reason": "截图中间区域显示手机号输入框"
}
```

### mobile_back / system_key（返回键 / 系统键）

- 触发 `Plan kind=back` / `system_key`；执行 `_run_mobile_back`（`184`）/ `_run_mobile_key`（`188`）。
- `_normalize_ai_step` 仅允许 `home/back/menu/power`（`3195-3199`）。
- 不需要坐标，保留。

### device_prepare / screen_ready（设备准备 / 亮屏解锁）

- 来源：移动引擎直连；`device_bootstrap.ensure_adb_device_online` / `engine.ensure_screen_ready`。
- 现状：prompt 第 14 条已禁止大模型规划解锁/唤醒/清后台等动作；`_run_mobile_click` 执行前会调 `engine.ensure_screen_ready`（`1865`）作为前置。
- **不是** AI 可调用 skill，只作执行前置事件。需记录到执行卡片和报告（待补）。

### shell_pm_clear（清理应用数据）

- 来源：移动引擎直连；`case_precondition_service._clear_app_data`。
- 只允许用例前置条件明确要求时调用，不能由普通 Copilot AI 自主触发。

### foreground_package（读取前台包名）

- 来源：移动引擎直连；`app_automation_service.guard_test_app_foreground`。
- 用于环境观察/用例校验，不能用它推导点击目标。

### screenshot（设备截图）

- 来源：`tools/screenshot`。
- AI 模式核心观察能力，由 Server 在 plan 前调用（`_build_ai_screen_context`）。
- 返回截图 URL/base64、原始尺寸、预览尺寸。

### ocr / dump_dom / hierarchy_clickables / clip_vision（本地定位通道）

- 现状：**AI 模式不使用**。仅 Local Plan / 调试 / 回归报告辅助使用。
- 这些通道由 `_resolve_click_target` 在 Local 模式下调用；AI 坐标分支完全不进入。

### sleep / assert（等待 / 断言）

- `sleep`：AI 可规划等待，建议限制最大时长。
- `assert`：用例执行链路保留；视觉断言走 AI 看图（`AI_CASE_ASSERT_SYSTEM_PROMPT` / `build_ai_assert_messages`，见下章），不用本地 OCR/DOM。

## 用例 / 回归通道的 AI 规划（原文档缺失）

除 Copilot 自由对话外，case/regression/feishu 通道也接入了大模型：

- 网关：`plan_message`（`4089`）在 `planning_mode=local` 且通道属于 `{case, case_execution, regression, feishu}` 时，调用 `system_settings_service.should_use_ai_planning`（`4107`）按 Key 配置决定是否自动切到 `mode=ai`。
- 单步规划 prompt：`AI_CASE_PLAN_SYSTEM_PROMPT`（`ai_plan_prompt.py:41`）——默认输出 1 个 step、强制坐标、禁止空 steps、禁止 `auto_run=false`。
- 预期校验：`AI_CASE_ASSERT_SYSTEM_PROMPT` + `build_ai_assert_messages`（`128/142`），基于截图语义判断预期是否成立，输出 `{passed, reply, reason, evidence}`。
- 超时：case 通道 90s，其余 45s（`_ai_plan_request_timeout`）。

## 实现现状与残留项

| # | 项 | 文件 | 状态 |
|---|---|---|---|
| 1 | Prompt 坐标化规则 | `ai_plan_prompt.py` | ✅ 已完成 |
| 2 | AI plan 前截图并传宽高 | `copilot_service.py:3243` | ✅ 已完成（Server 端，非前端） |
| 3 | preview→device 坐标映射 | `copilot_service.py:3292` | ✅ 已完成 |
| 4 | `_normalize_ai_step` 缺坐标拒绝 | `copilot_service.py:3048,4004` | ✅ 已完成 |
| 5 | `_run_mobile_click` 坐标专用分支 | `copilot_service.py:1899` | ✅ 已完成 |
| 6 | `execute_steps` 按 `ai_coordinate_only` 分流 | `copilot_service.py:4427,4708,4734` | ✅ 已完成 |
| 7 | Tool Catalog 拆 local/ai | `skills_registry.py` | ✅ 已删除（整条死链路已移除） |

### 残留项 / 建议改动点

1. ~~**Tool Catalog `local_tools`/`ai_tools` 拆分已不适用**~~ — ✅ 已删除 `list_anthropic_tool_use_catalog`、后端 `/settings/skills/tools/anthropic` 端点、前端 `getAnthropicToolUseCatalog`。

2. ~~**AI 模式 `input` 仍透传 `field_hint`**~~ — ✅ 已改：`require_visual_coordinates=True` 时不再带 `field_hint`。

3. ~~**`coords_explicit` 推断偏宽松**~~ — ✅ 已改：改为 `(x > 0 and y > 0)`。

4. **日志样例需对齐真实格式**。引擎实际打印（`mAdb.py:1112`）：
   `Gesture audit tap ({x},{y}) label={label!r} skip_label=True consent=... serial=...`，比原验收样例多 `label/consent/serial` 字段。

5. ~~**AI 观察截图未进执行卡片**~~ — ✅ 已接：`build_plan_log` 写入 `screen_observe`；`build_operation_plan_tree` 在 `flat_items` 首部插入 `observe` 并暴露 `observe_screen`；`feishu_regression_service` 操作/断言块回退使用观察图；前端 `ExecutionReplayer` / `Dialogue` 展示 👁 观察卡片。

## AI 用例执行的 local 前置边界（架构重构后）

用例执行选 AI（`should_use_ai_planning("case_execution")`）时，`AiExecutionProfile` 会跳过以下本地写死逻辑：

- `_prepare_case_screen_for_ai_plan` / `run_overlay_guard_on_device`（规划前本地清弹窗）
- `execute_steps` 内 reactive overlay guard（`enable_overlay_guard=false`）
- `_verify_step_expected` 内本地 OCR / `identify_for_app` / `identify_page_for_trace` / 本地 `_check_expected`（改走 `_verify_step_expected_ai` 仅 LLM 断言）
- `ensure_page_ready_before_action`（操作前页面恢复 / OCR 识别 / 本地恢复步骤）
- `_prepare_screen_for_verify` → `run_overlay_guard_until_clear`（校验前本地清弹窗）
- `try_recover_and_reverify`（校验失败后的本地页面恢复）
- 操作失败后的 `try_dismiss_blocking_overlay`（本地 post-recovery）

弹窗、consent / 权限 / 协议页、PageNavigation 恢复均由大模型 Plan 自行规划点击；本地 Overlay Guard 与 PageNavigation 仅保留在 Local 模式。

**仍保留的 shared 底座**（非本地业务判断）：`ensure_adb_device_online`、`ensure_screen_ready`（亮屏解锁）、`guard_test_app_foreground`（前台观察）。

**目录分层**（`refactor/services-ai-local-shared` 分支）：

- `server/services/ai/` — 大模型 prompt / 规划 / `AiExecutionProfile`
- `server/services/local/` — 定位、导航、弹窗守卫、本地规则 / `LocalExecutionProfile`
- `server/services/shared/` — 截图、页面识别、设备、语义工具 / `execution_profile` 协议
- `server/services/executor/` — 执行层（`mobile_actions`、`execute_steps`、`locate_debug`）
- 顶层 `copilot_service.py` — 规划 + 本地定位解析；`feishu_regression_service.py` — 用例编排

**ExecutionProfile**（`shared/execution_profile.py`）：

- `resolve_execution_profile(channel)` 按 `should_use_ai_planning` 返回 `AiExecutionProfile` 或 `LocalExecutionProfile`
- `AiExecutionProfile`：`before_action` / `before_verify` / 本地 recovery 均为 no-op
- `LocalExecutionProfile`：委托 `ensure_page_ready_before_action`、`run_overlay_guard_until_clear` 等本地逻辑

## 验收标准

### AI 模式成功日志应该长这样

```text
copilot/chat planning_mode=ai provider_id=umodelverse
AI raw_plan steps=[{"kind":"click","x":449,"y":2492,"coords_explicit":true}]
copilot/execute steps=[{"kind":"click","x":449,"y":2492,"coords_explicit":true,"ai_coordinate_only":true}]
CopilotService: AI coordinate click x=449 y=2492 label=... skip local locate
MAdbEngine: Gesture audit tap (449,2492) label='造物秀' skip_label=True consent=False serial=...
```

### AI 模式不应该再出现

```text
OCR
ClipLocate
LocateIconRow
LocateArbitrator
click_by_label
method=ocr
method=clip_icon_row
coords=(0,0)
coords_explicit=False
```

### AI 缺坐标时应该失败

如果 AI 返回：

```json
{ "kind": "click", "label": "首页" }
```

Server 返回（`copilot_service.py:4037-4060`）：

```json
{
  "auto_run": false,
  "ai_error_info": {
    "type": "missing_coordinates",
    "title": "大模型未返回坐标",
    "message": "AI 模式下 click/input/swipe 必须返回坐标，label 只能用于审计展示。",
    "suggestion": "请重试或换用支持视觉输入的模型；如果截图不清晰，可先等待页面稳定后再执行。"
  }
}
```

不执行，不 fallback 到 local。
