# 页面类型（PageProfile）

文件：`server/services/locate/page_profiles.py`

## 为什么需要页面类型

同一指令在不同页面上通道可信度不同：

| 页面 | 典型 UI | 更可信通道 |
|------|---------|------------|
| login | 无字图标行、大按钮、协议勾选 | icon_row、gallery、clip |
| home | 信息流卡片、顶部分段 | ocr、hierarchy |
| profile | 文字列表、设置项 | ocr、hierarchy |
| settings | 纯文字列表 | ocr、hierarchy |

login 只是 **众多 PageProfile 之一**，不是特殊硬编码管道。

## 内置 Profile

| key | 标题 | 识别依据（节选） | prefer_icon_row |
|-----|------|------------------|-----------------|
| `consent` | 隐私同意弹窗 | 「不同意」「造物者」「隐私条款」 | 否 |
| `system_dialog` | 系统权限弹窗 | 「仅在使用中允许」「是否允许」 | 否 |
| `login` | 登录页 | label/屏文含「登录」「一键登录」「访客浏览」 | 是 |
| `home` | 首页 Feed | 「首页」「推荐」「造物秀」 | 否 |
| `profile` | 我的 | 「我的」「个人」「设置」 | 否 |
| `settings` | 设置页 | 「设置」「偏好」 | 否 |
| `generic` | 默认 | 未命中以上 | 否 |

`consent` / `system_dialog` 主要由 [Overlay Guard](./overlay-guard.md) 在业务 Plan 前触发。

## ChannelWeights 字段

```python
ChannelWeights(
    clip=0.30,       # CLIP 全屏/区域 patch
    ocr=0.30,        # OCR 文本框
    hierarchy=0.25,  # 无障碍树文案
    gallery=0.35,    # 应用图标库 embedding
    icon_row=0.35,   # 通用无字图标行 + CLIP
)
```

## 解析流程

```python
from server.services.locate.page_profiles import resolve_page_profile

profile = resolve_page_profile(
    page_context=pc,      # identify_page_for_trace 返回值
    screen_text=ocr_blob,
)
```

1. 读 `page_context.label` / `figma_best`  
2. 用各 profile 的 `label_patterns`、`screen_text_patterns` 正则匹配  
3. 首个命中者生效，否则 `generic`  

## 与图谱 / Figma 的关系

`page_context_service` 已提供页面识别（骨架、Figma 文案）。Locate 模块 **消费** 其 `label`，不重复实现识别算法。

后续可将图谱节点 `metadata.page_type` 或 Figma frame 名直接映射为 profile key，见 [extending.md](./extending.md)。
