# 通用无字图标行（Icon Row）

文件：`server/services/locate/icon_row.py`

## 与原 `login_row` 的区别

| | 旧 `login_row` | 新 `icon_row` |
|---|----------------|---------------|
| 适用范围 | 主要登录页 | **任意页面** |
| Y 范围 | 固定 66%–88% 屏高 | **全屏**聚类 |
| 触发 | `infer_region_hint` 关键词 | `PageProfile.prefer_icon_row` + `TargetKind.ICON` |
| 实现 | `clip_locate_service._locate_login_row` | hierarchy 聚类 + CLIP patch 打分 |

## 检测规则

从 hierarchy 可点击节点中筛选：

- 宽高的上限相对屏宽/高（非固定像素带）  
- 排除长中文文案（协议、用户协议等）  
- 按 `cy` 聚类为水平行，每行 ≥ 2 个图标  

输出按 **x 从左到右** 排序，供 CLIP 对每个 patch 打分。

## 何时启用

1. `PageProfile.prefer_icon_row == True`（默认 login）  
2. 或 `TargetKind.ICON`（指令含「xx登录方式」「图标」等）  

其他页面若存在工具栏图标行，可将对应 profile 的 `prefer_icon_row` 设为 `True`。

## 日志

```
LocateIconRow: detected 2 icon row(s) sizes=[3, 4]
LocateArbitrator: pick channel=icon_row method=clip_icon_row ...
```

## 与图标库配合

无字图标应同时在 **设置 → 无字图标** 中维护 embedding；`gallery` 通道与 `icon_row` 通道会共同竞争，仲裁器取得分更高者。
