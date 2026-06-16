# AI Plan Skills 与坐标化改造说明

## 结论

当前大模型模式仍然会触发本地 OCR、CLIP、Hierarchy 和 LocateArbitrator，不是因为前端没有选择大模型，而是因为现有协议允许大模型返回 `label` 型点击：

```json
{
  "kind": "click",
  "x": 0,
  "y": 0,
  "label": "首页",
  "coords_explicit": false,
  "summary": "点击文本「首页」"
}
```

执行器收到这种 step 后只能用本地能力把 `label` 解析成坐标，所以日志里会继续出现：

- `OCR`
- `ClipLocate`
- `LocateIconRow`
- `LocateArbitrator`
- `method=ocr`
- `method=clip_icon_row`
- `skip_label_lookup=False`

如果目标是“所有目标判断交给 AI”，AI 模式下就不能再允许 `label -> 本地定位`。AI 必须基于当前屏幕截图输出明确坐标，执行器只负责注入坐标。

## 目标边界

### AI 负责

- 看当前屏幕截图。
- 判断用户意图对应的视觉目标。
- 输出绝对屏幕坐标 `x/y`。
- 可选输出 `bbox`、`label`、`confidence`、`reason` 用于审计。
- 判断无法安全定位时，返回 blocker，不允许让执行器兜底定位。

### 执行器负责

- 提供当前截图和屏幕尺寸给 AI。
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

## 为什么之前拆不出来

### 1. Prompt 曾经要求使用本地语义定位

旧版 `server/services/ai_plan_prompt.py` 规则写的是：

```text
Android 移动端优先使用语义定位/多通道点击，不要自行发明固定坐标；只有用户明确给坐标时才使用坐标。
click 必须填写 label。
```

这会直接诱导大模型返回 `label`，而不是 `x/y`。

现已调整为：大模型模式必须基于截图输出坐标；Local Plan 不使用这个大模型 prompt，仍可保留本地规则能力。

### 2. AI 请求没有当前截图

旧版 `copilot/chat` 请求只包含：

```json
{
  "text": "点击首页",
  "sn": "5fda2f6d",
  "context": { "platform": "android" },
  "planning_mode": "ai",
  "provider_id": "umodelverse"
}
```

AI 没有拿到当前截图，也没有屏幕尺寸，所以它无法可靠输出坐标。它只能把自然语言翻译成 `label` 型 step。

现已调整为：AI Plan 前由后端截取当前设备画面，压缩成低清晰度 JPEG base64，并把原始屏幕宽高一起传给大模型。AI 必须把坐标映射回原始屏幕尺寸。

### 3. `_normalize_ai_step` 兼容了 label 型视觉动作

旧逻辑中，AI 返回 `kind=click,label=首页` 后，标准化层会继续保留为合法 step，而不是拒绝。

现已调整为：AI 模式下 click/input/swipe 等视觉动作必须有显式坐标；`label` 仍然保留，但只用于审计展示。

### 4. `_run_mobile_click` 默认会解析 label

`_run_mobile_click` 会调用 `_resolve_click_target`，后者会依次尝试：

- 特殊协议/弹窗/底栏规则。
- Hierarchy 可点击节点。
- OCR 文本候选。
- CLIP / icon row。
- LocateArbitrator。
- `engine.click_by_label`。

只要 `coords_explicit=false` 或 `x/y=0`，就会进入本地定位路径。

现已调整为：AI 标准化后的视觉 step 会带上 `ai_coordinate_only=true`；执行层看到该标记后会直接走坐标注入，不进入 `_resolve_click_target`。

## 当前 Skills 清单

### window_start

- 名称：启动应用
- 当前来源：`skills_registry.py` 的 `public/window`
- 当前触发：`Plan kind=open_app`
- 当前调用：Tentacle `public/window`
- 是否参与本地判断：否
- AI 坐标化后是否保留：保留
- 调整建议：保留为 AI 可用 skill，但参数必须明确包名、Bundle、URL 或应用名。不能靠本地猜测目标应用。

### window_close

- 名称：关闭应用
- 当前来源：`skills_registry.py` 的 `public/window`
- 当前触发：`Plan kind=close_app`
- 当前调用：Tentacle `public/window`
- 是否参与本地判断：否
- AI 坐标化后是否保留：保留
- 调整建议：保留。需要 before/after 截图记录。

### window_switch

- 名称：切换应用
- 当前来源：`skills_registry.py` 的 `public/window`
- 当前调用：Tentacle `public/window`
- 是否参与本地判断：否
- AI 坐标化后是否保留：保留
- 调整建议：需要明确目标应用标识。不能用当前页面视觉判断代替应用标识。

### gesture_click

- 名称：点击 / 长按
- 当前来源：`skills_registry.py` 的 `public/gesture`
- 当前触发：`Plan kind=click`
- 当前调用：`copilot_service._run_mobile_click`
- 当前问题：既支持坐标，也支持 `label` 定位；移动端优先走本地定位仲裁。
- 是否参与本地判断：是
- AI 坐标化后是否保留：保留，但必须改为坐标型 skill。
- 调整建议：
  - 改名或新增 `visual_click`。
  - AI 模式下必填 `x/y`。
  - `label` 只允许审计展示。
  - 必须携带 `coords_explicit=true`。
  - 执行器必须跳过 `_resolve_click_target`。

目标协议：

```json
{
  "kind": "click",
  "x": 449,
  "y": 2492,
  "coords_explicit": true,
  "label": "造物秀",
  "bbox": { "x": 410, "y": 2450, "w": 90, "h": 60 },
  "confidence": 0.86,
  "reason": "截图底部导航栏右侧显示「造物秀」入口"
}
```

### gesture_swipe

- 名称：滑动
- 当前来源：`skills_registry.py` 的 `public/gesture`
- 当前触发：`Plan kind=swipe`
- 当前调用：`copilot_service._run_mobile_swipe`
- 当前问题：只支持方向语义 `up/down/left/right`，执行器内部生成坐标。
- 是否参与本地判断：弱参与。方向由 AI/规则判断，坐标由执行器生成。
- AI 坐标化后是否保留：保留，但应改为坐标型。
- 调整建议：
  - 新增 `visual_swipe`。
  - AI 输出 `start_x/start_y/end_x/end_y/duration_ms`。
  - 方向 `direction` 只能作为展示字段，不能作为唯一执行参数。

目标协议：

```json
{
  "kind": "swipe",
  "start_x": 600,
  "start_y": 1900,
  "end_x": 600,
  "end_y": 850,
  "duration_ms": 350,
  "reason": "需要向上滑动列表查看更多内容"
}
```

### mobile_input

- 名称：文本输入
- 当前来源：`skills_registry.py` 的移动引擎直连
- 当前触发：`Plan kind=input`
- 当前调用：`copilot_service._run_mobile_input`
- 当前问题：会依赖 field hint、上一步 click 的 `target_rect`、u2 EditText、焦点查找。
- 是否参与本地判断：是
- AI 坐标化后是否保留：保留，但需要改为坐标型输入。
- 调整建议：
  - 新增 `visual_input`。
  - AI 必须先给输入框坐标。
  - 执行器点击坐标后只负责输入文本。
  - 如果点击后没有焦点，可以失败并要求 AI 重新判断，不能本地找输入框。

目标协议：

```json
{
  "kind": "input",
  "x": 520,
  "y": 1180,
  "text": "13800138000",
  "coords_explicit": true,
  "label": "手机号输入框",
  "reason": "截图中间区域显示手机号输入框"
}
```

### mobile_back

- 名称：返回键
- 当前来源：移动引擎直连
- 当前触发：`Plan kind=back`
- 当前调用：`_run_mobile_back` / `_run_mobile_key`
- 是否参与本地判断：否
- AI 坐标化后是否保留：保留
- 调整建议：保留。系统键不需要坐标。

### device_prepare

- 名称：设备准备 / 唤醒解锁
- 当前来源：移动引擎直连
- 当前调用：`device_bootstrap.ensure_adb_device_online` / `engine.ensure_screen_ready`
- 当前触发：执行前设备未就绪、黑屏、锁屏、连接恢复
- 是否参与本地判断：不应该参与 Plan，但会影响执行前状态
- AI 坐标化后是否保留：只作为执行前置事件保留
- 调整建议：
  - 不能作为 AI 可调用 skill。
  - 必须记录到执行卡片和报告。
  - 不能静默执行后不展示。

### shell_pm_clear

- 名称：清理应用数据
- 当前来源：移动引擎直连
- 当前调用：`case_precondition_service._clear_app_data`
- 是否参与本地判断：否
- AI 坐标化后是否保留：谨慎保留
- 调整建议：只允许用例前置条件明确要求时调用，不能由普通 Copilot AI 自主触发。

### screen_ready

- 名称：亮屏解锁
- 当前来源：移动引擎直连
- 当前调用：`engine.ensure_screen_ready`
- 是否参与本地判断：不应参与 Plan
- AI 坐标化后是否保留：只作为执行器前置事件
- 调整建议：从 AI tools 中移除；执行器事件必须显式记录。

### foreground_package

- 名称：读取前台包名
- 当前来源：移动引擎直连
- 当前调用：`app_automation_service.guard_test_app_foreground`
- 是否参与本地判断：用于环境观察，不是点击定位
- AI 坐标化后是否保留：作为观察/断言能力保留
- 调整建议：仅用于报告与用例校验，不能用它推导点击目标。

### screenshot

- 名称：设备截图
- 当前来源：`tools/screenshot`
- 当前调用：Tentacle `tools/screenshot` / `regression_capture`
- 当前触发：定位通道、回归截图、Plan ability
- 是否参与本地判断：当前间接参与，给 OCR/CLIP/回放使用
- AI 坐标化后是否保留：必须保留
- 调整建议：
  - 改造成 AI 模式的核心观察 skill：`screen_observe`。
  - 返回截图 URL/base64、屏幕尺寸、时间戳。
  - AI 每次 plan 前必须拿到最新截图。

目标观察协议：

```json
{
  "screen": {
    "image_url": "http://127.0.0.1:10104/static/xxx.png",
    "width": 1200,
    "height": 2608,
    "captured_at": "2026-06-16T12:01:19.000"
  }
}
```

### ocr

- 名称：屏上 OCR
- 当前来源：`tools/ocr`
- 当前调用：OCR 服务
- 当前触发：多通道定位 OCR 通道
- 是否参与本地判断：是
- AI 坐标化后是否保留：不应作为 AI Plan skill 保留
- 调整建议：
  - 从 AI tools 中移除。
  - 只保留为 Local Plan、调试、回归报告辅助能力。
  - AI 模式不允许用 OCR 候选替代 AI 判断。

### dump_dom

- 名称：布局树
- 当前来源：`tools/dump_dom`
- 当前调用：Android hierarchy / web DOM
- 是否参与本地判断：是
- AI 坐标化后是否保留：不应作为 AI Plan skill 保留
- 调整建议：
  - 从 AI tools 中移除。
  - 只保留为 Local Plan、调试、报告辅助能力。
  - 如果未来给 AI 使用，也必须作为上下文，不允许执行器用它兜底定位。

### keyevent

- 名称：按键事件
- 当前来源：`tools/keyevent`
- 当前调用：系统 keyevent
- 是否参与本地判断：否
- AI 坐标化后是否保留：保留
- 调整建议：保留 `system_key`，明确支持 `home/back/menu/power`。

### sleep

- 名称：等待
- 当前来源：`cfs/sleep`
- 是否参与本地判断：否
- AI 坐标化后是否保留：保留
- 调整建议：AI 可以规划等待，但需要限制最大时长，避免长时间卡住。

### assert

- 名称：断言
- 当前来源：`cfs/mAssert`
- 是否参与本地判断：用于用例检查
- AI 坐标化后是否保留：保留在用例执行链路
- 调整建议：
  - Copilot 普通执行不需要每步断言。
  - 用例执行仍然每步后做预期检查。
  - 如果断言需要视觉判断，应也走 AI 看图，而不是本地 OCR/DOM 判断。

### hierarchy_clickables

- 名称：Hierarchy 可点击遍历
- 当前来源：定位内部通道
- 当前调用：`locate.channels.collect_text_channels`
- 当前触发：点击定位多通道
- 是否参与本地判断：是
- AI 坐标化后是否保留：从 AI skills 中移除
- 调整建议：只允许 Local Plan 使用。AI 模式必须禁用。

### clip_vision

- 名称：CLIP 视觉匹配
- 当前来源：定位内部通道
- 当前调用：`clip_locate_service` / `icon_row`
- 当前触发：无字图标、登录 icon 行、底栏入口
- 是否参与本地判断：是
- AI 坐标化后是否保留：从 AI skills 中移除
- 调整建议：只允许 Local Plan 使用。AI 模式必须禁用。

## 新的 AI Skills 目录建议

### screen_observe

- 类型：观察
- 谁调用：Server 在请求 AI Plan 前调用
- 输入：`sn`、`platform`
- 输出：截图 URL/base64、屏幕尺寸
- AI 是否可直接下发：否
- 作用：给 AI 判断目标坐标

### visual_click

- 类型：动作
- 谁判断：AI
- 谁执行：执行器
- 必填：`x`、`y`、`coords_explicit=true`
- 可选：`label`、`bbox`、`confidence`、`reason`
- 禁止：只给 `label` 不给坐标

### visual_input

- 类型：动作
- 谁判断：AI
- 谁执行：执行器
- 必填：`x`、`y`、`text`、`coords_explicit=true`
- 行为：先点击坐标，再输入文本
- 禁止：执行器按 `field_hint` 本地找输入框

### visual_swipe

- 类型：动作
- 谁判断：AI
- 谁执行：执行器
- 必填：`start_x`、`start_y`、`end_x`、`end_y`
- 可选：`duration_ms`、`reason`
- 禁止：只返回 `direction`

### system_key

- 类型：动作
- 谁判断：AI
- 谁执行：执行器
- 必填：`key`
- 支持：`home`、`back`、`menu`、`power`

### open_app / close_app

- 类型：动作
- 谁判断：AI 或用户明确指定
- 谁执行：执行器
- 必填：包名、Bundle、URL 或明确 app id

### wait

- 类型：控制
- 谁判断：AI
- 谁执行：执行器
- 必填：`duration_ms`

### device_prepare_event

- 类型：前置事件
- 谁判断：执行器
- 谁执行：执行器
- AI 是否可下发：否
- 要求：必须显示到卡片和报告

## 必须修改的代码点

### 1. Prompt

文件：`server/services/ai_plan_prompt.py`

需要删除：

```text
Android 移动端优先使用语义定位/多通道点击，不要自行发明固定坐标；只有用户明确给坐标时才使用坐标。
click 必须填写 label。
```

需要改为：

```text
AI 模式必须基于当前截图输出坐标。click/input/swipe 不允许只输出 label。
label 只用于展示和审计，执行器不会使用 label 定位。
如果没有截图或无法判断坐标，返回 blocker，不要生成可执行步骤。
```

### 2. AI Plan 上下文

文件：`src/views/Dialogue/index.vue`、`server/websocket/routers/wCopilot.py`、`server/services/copilot_service.py`

需要在 `copilot/chat` 前获取截图，并把截图 URL/base64、宽高传给大模型。

### 3. `_normalize_ai_step`

文件：`server/services/copilot_service.py`

AI 模式下需要校验：

- `kind=click` 必须有有效 `x/y`。
- `kind=input` 必须有有效 `x/y/text`。
- `kind=swipe` 必须有起止坐标。
- 只有 `label` 的 step 必须拒绝。
- 拒绝后返回 AI 错误卡片，不执行。

### 4. `_run_mobile_click`

文件：`server/services/copilot_service.py`

需要新增 AI 坐标模式：

- `ai_coordinate_only=true`
- 直接校验坐标范围。
- 直接 `engine.click(position=(x,y), skip_label_lookup=True)`。
- 禁止调用 `_resolve_click_target`。
- 禁止进入 OCR/CLIP/Hierarchy/LocateArbitrator。

### 5. `execute_steps`

文件：`server/services/copilot_service.py`

需要把 planner 信息或 `ai_coordinate_only` 传到执行层。AI steps 执行时只允许坐标执行；Local Plan 执行时才允许本地多通道定位。

### 6. Tool Use Catalog

文件：`server/services/skills_registry.py`

需要拆成两个目录：

- `local_tools`：OCR、CLIP、Hierarchy、LocateArbitrator、本地语义定位。
- `ai_tools`：screen_observe、visual_click、visual_input、visual_swipe、system_key、open_app、close_app、wait。

AI provider 只能看到 `ai_tools`。

## 验收标准

### AI 模式成功日志应该长这样

```text
copilot/chat planning_mode=ai provider_id=umodelverse
AI raw_plan steps=[{"kind":"click","x":449,"y":2492,"coords_explicit":true}]
copilot/execute steps=[{"kind":"click","x":449,"y":2492,"coords_explicit":true,"ai_coordinate_only":true}]
CopilotService: AI coordinate click x=449 y=2492 skip local locate
MAdbEngine: Gesture audit tap (449,2492) skip_label=True
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

应该返回：

```json
{
  "auto_run": false,
  "ai_error_info": {
    "title": "大模型未返回坐标",
    "message": "AI 模式下 click 必须包含 x/y，label 只能用于审计。"
  }
}
```

不能执行，不能 fallback 到 local。
