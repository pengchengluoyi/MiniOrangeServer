# UI 定位能力（Locate）

本目录说明 MiniOrange 服务端 **统一点击定位** 模块：`server/services/locate/`。

## 解决什么问题

飞书用例中的自然语言步骤（如「登录页面点击右上角的访客浏览」「点击手机号登录方式」）需要映射到屏幕坐标。旧实现是 **固定管道 + 大量场景 hardcode**（`login_row`、底栏 Tab、无障碍 label 等）。

新模块采用：

1. **多通道并行打分**：CLIP、OCR、Hierarchy、图标库、通用无字图标行  
2. **仲裁取最高**：各通道算分后按 PageProfile 权重排序，**取第一名**（无全局分数门槛）  
3. **空间约束**：仅 `spatial.py` 九宫格方位词过滤；扫描本身 **全屏**  
4. **页面类型 Profile**：login / home / consent / generic 等，YAML 可扩展  

**已移除主路径**：Y 带裁剪、`_kind_channel_boost`、`LOCATE_MIN_SCORE` 准入、底栏 Tab 专用解析、登录 `login_row` 固定 Y 带。

## 快速开关

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `LOCATE_ARBITRATOR` | `1` | `0` 时回退旧版 `_resolve_click_target` 管道 |
| `CLIP_ENABLED` | `1` | 关闭后 CLIP / 图标行 CLIP 通道不参与 |
| `LOCATE_PROFILES_PATH` | 内置 YAML | 自定义页面 profile 权重包 |

## 入口

```python
from server.services.locate import resolve_locate_target

pos, method, detail, rect = resolve_locate_target(
    engine, screen_w, screen_h,
    label="登录页面点击右上角的访客浏览",
    icon_targets=[...],
    page_context=identify_page_for_trace(...),  # 可选
)
```

Copilot 在 `_resolve_click_target` 中已默认调用（`LOCATE_ARBITRATOR=1`）。

## 阻塞弹窗守卫

业务 **点击失败且屏被阻塞** 时，反应式插入 **守卫 Plan + 单次 Tap**，再重试业务 Plan；与 Locate 共用通道与 `locate_debug`。  
**不是**在业务 Plan 之前批量 Detect/Recheck。

阻塞业务定位时返回 `blocked_overlay`，文案含具体弹窗类型（见 `blocked_overlay_message`）。

详见 [overlay-guard.md](./overlay-guard.md)。用例「预期 N」校验见 [../regression/expectation-assert.md](../regression/expectation-assert.md)。

## 文档索引

| 文件 | 内容 |
|------|------|
| **[strategy.md](./strategy.md)** | **多通道定位策略总览（profile/kind/通道/阈值，供评审）** |
| [app-packages.md](./app-packages.md) | 主流应用包名注册表（微信/QQ/WhatsApp/X/小红书…） |
| [page-profiles.md](./page-profiles.md) | 页面类型 profile 与 YAML 资源包 |
| [architecture.md](./architecture.md) | 架构、通道、仲裁公式 |
| [overlay-guard.md](./overlay-guard.md) | Plan 内阻塞弹窗守卫 |
| [spatial-zones.md](./spatial-zones.md) | 九宫格空间约束 |
| [icon-row.md](./icon-row.md) | 通用无字图标行（原 login_row 能力扩展） |
| [extending.md](./extending.md) | 扩展页面类型、自定义权重、与图谱/Figma 集成 |
| [../regression/expectation-assert.md](../regression/expectation-assert.md) | 飞书用例预期校验 |
