# 空间约束（九宫格）

文件：`server/services/locate/spatial.py`

## 设计原则

- 方位词只产生 **几何过滤条件**，不决定使用 CLIP 还是 OCR  
- 支持 **多 zone 并集**：点在任一 zone 内即通过  
- 与页面类型无关：任何页面都可说「右上角」「底部中间」

## 九宫格定义（相对屏宽/高）

| Zone | X | Y |
|------|---|---|
| `top_left` | 0–34% | 0–34% |
| `top_center` | 33–67% | 0–34% |
| `top_right` | 66–100% | 0–34% |
| `middle_left` | 0–34% | 33–67% |
| `center` | 33–67% | 33–67% |
| `middle_right` | 66–100% | 33–67% |
| `bottom_left` | 0–34% | 66–100% |
| `bottom_center` | 33–67% | 66–100% |
| `bottom_right` | 66–100% | 66–100% |

## 自然语言映射示例

| 用语 | 映射 zones |
|------|------------|
| 右上角、右上 | `top_right` |
| 左上角、左上 | `top_left` |
| 右下角、右下 | `bottom_right` |
| 左下角、左下 | `bottom_left` |
| 中间、正中、居中 | `center` |
| 中上、上中 | `top_center` |
| 中下、下中 | `bottom_center` |
| 左侧、左边 | `top_left` + `middle_left` + `bottom_left` |
| 右侧、右边 | `top_right` + `middle_right` + `bottom_right` |
| 顶部、上方 | 顶行三格 |
| 底部、底栏、下方 | 底行三格 |

## 核心文案剥离

输入：`登录页面点击右上角的访客浏览`

解析结果：

- `zones`: `{top_right}`  
- `core_text`: `访客浏览`  

所有通道对 `访客浏览` 打分，再经 `top_right` 过滤。

## API

```python
from server.services.locate.spatial import parse_spatial_constraint, point_in_zones

spatial = parse_spatial_constraint(label)
ok = point_in_zones(cx, cy, screen_w, screen_h, spatial.zones)
```
