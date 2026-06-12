# 通用无字图标行（Icon Row）

文件：`server/services/locate/icon_row.py`

## 与原 `login_row` 的区别

| | 旧 `login_row` | 新 `icon_row` |
|---|----------------|---------------|
| 适用范围 | 主要登录页 | **任意页面** |
| Y 范围 | 固定 66%–88% 屏高 | **全屏**聚类 |
| 触发 | `infer_region_hint` + 固定 Y 带 | **全屏**；`gather_all_candidates(enable_icon_row=True)` |
| 实现 | `clip_locate_service._locate_login_row` | `icon_row.py` + `channels.collect_icon_row_channel` |

## 检测规则

从 **全屏** hierarchy 可点击节点中筛选：

- 尺寸像图标（宽高上限相对屏宽/高，非 Y 带）  
- 排除长中文文案（协议链接等）  
- 按 `cy` 聚类为水平行，每行 ≥ 2 个图标  

输出按 **x 从左到右** 排序，CLIP 对每个 patch 打分，top-5 进入候选池。

## 何时参与仲裁

- `resolver` 默认 `enable_icon_row=True`，与 profile **无关**  
- `page_profiles.yaml` 中 `prefer_icon_row` **已废弃**（仅保留字段兼容，不门控收集）  
- 与 OCR / CLIP / gallery 等通道按权重公平竞争，取得分最高者

## 日志

```
LocateIconRow: detected 2 icon row(s) sizes=[3, 4]
LocateArbitrator: pick channel=icon_row method=clip_icon_row ...
```

## 与图标库配合

无字图标应同时在 **设置 → 无字图标** 中维护 embedding；`gallery` 通道与 `icon_row` 通道会共同竞争，仲裁器取得分更高者。
