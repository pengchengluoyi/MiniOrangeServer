# 扩展指南

## 1. 注册新页面类型

```python
from server.services.locate.page_profiles import (
    ChannelWeights,
    PageProfile,
    register_page_profile,
)

register_page_profile(
    PageProfile(
        key="checkout",
        title="收银台",
        label_patterns=[r"支付", r"checkout", r"订单确认"],
        screen_text_patterns=[r"立即支付", r"微信支付"],
        weights=ChannelWeights(clip=0.25, ocr=0.38, hierarchy=0.30, gallery=0.28, icon_row=0.15),
        prefer_icon_row=False,
        description="支付页以文字按钮为主",
    )
)
```

在应用启动或插件加载时调用一次即可。

## 2. 从图谱节点绑定 page_type

推荐在 `AppNode` 扩展字段或 `skeleton_config` 中增加：

```json
{ "page_type": "login" }
```

在 `resolve_page_profile` 调用前，若 `page_context.node_id` 可查到节点，优先用节点上的 `page_type` 作为 profile key。

（此接线可在业务层完成，无需改 Locate 核心。）

## 3. 向量化 / 市面方案对照

| 能力 | 本项目实现 | 外部可选方案 |
|------|------------|--------------|
| 页面类型识别 | 图谱 skeleton + Figma 文案 + OCR | 自训练页面分类器、Appium 页面 source fingerprint |
| 文字定位 | OCR + hierarchy 文本相似度 | PaddleOCR、Google ML Kit |
| 图标/区域语义 | OpenCLIP ViT-B-32 | Grounding DINO、OmniParser、SeeClick |
| 图标库 | 自维护截图 + CLIP embedding | 向量库（Milvus）存 patch embedding |
| 空间约束 | 规则解析九宫格 | VLM 输出 bounding box（如 GPT-4V） |

Locate 模块设计为 **可替换通道**：新增通道只需在 `channels.py` 实现 `collect_*_channel`，产出 `LocateCandidate`，并在 `ChannelWeights` 增加权重字段。

## 4. 调试

1. 设置 `LOCATE_ARBITRATOR=1`  
2. 查看日志标签：`LocateResolver`、`LocateChannels`、`LocateArbitrator`、`LocateIconRow`  
3. 调低/调高 `LOCATE_MIN_SCORE` 观察误点与漏点  
4. 对比 `LOCATE_ARBITRATOR=0` 与旧管道行为  

## 5. 不建议扩展的方向

- 在 Locate 内再增加 **Tab 专用** 或 **无障碍 label** 通道（主路径已明确移除）  
- 为单个 App 写 `if app_id == ...` 坐标 — 应走图标库或图谱  

## 6. 相关代码

- Copilot 集成：`server/services/copilot_service.py` → `_resolve_click_target`  
- 旧 CLIP 区域：`server/services/clip_locate_service.py`（逐步仅作 CLIP 通道底层）  
- 页面识别：`server/services/page_context_service.py`  
