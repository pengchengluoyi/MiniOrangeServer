# 页面类型（PageProfile）

> 前台 App 识别（包名 → 微信/QQ/WhatsApp/X/小红书…）见 [app-packages.md](./app-packages.md)。

- **资源包（关键词 / 权重）**：`server/resources/locate/page_profiles.yaml`  
- **加载逻辑**：`server/services/locate/page_profiles.py`  
- **自定义路径**：环境变量 `LOCATE_PROFILES_PATH=/path/to/page_profiles.yaml`

修改 YAML 后重启服务；无需改 Python 即可增删 profile、调整正则与通道权重。

## 为什么需要页面类型

同一指令在不同页面上通道可信度不同：

| 页面 | 典型 UI | 更可信通道 |
|------|---------|------------|
| login | 无字图标行、大按钮、协议勾选 | icon_row、gallery、clip |
| home | 信息流卡片、顶部分段 | ocr、hierarchy |
| profile | 文字列表、设置项 | ocr、hierarchy |
| settings | 纯文字列表 | ocr、hierarchy |

login 只是 **众多 PageProfile 之一**，不是特殊硬编码管道。

## 内置 Profile（v2 资源包，共 22 类）

完整关键词见 `server/resources/locate/page_profiles.yaml`。匹配顺序：**自上而下首个命中**；`bootstrap_rules` 的 OCR 规则优先于 profile 列表。

### 登录 / 注册链路

| key | 标题 | 典型识别信号 |
|-----|------|----------------|
| `verify_code` | 验证码输入页 | 输入验证码、重新获取、验证码已发送 |
| `phone_register` | 手机号注册页 | 注册账号、设置密码、立即注册 |
| `password_login` | 账号密码登录页 | 密码登录、忘记密码、用户名或邮箱 |
| `phone_login` | 手机号登录页 | 手机号登录、请输入手机号、获取验证码 |
| `one_click_login` | 一键登录页 | 本机号码一键登录、运营商认证 |
| `bind_phone` | 绑定手机号页 | 绑定手机、换绑 |
| `login` | 登录入口 / 方式选择 | 登录注册页、访客浏览、第三方图标行 |

步骤文案还可通过 `profile_key_for_login_step()` 细化为上述 key（见 `resolver.py`）。

### 弹窗 / 引导 / 条款

| key | 标题 |
|-----|------|
| `consent` | 隐私同意弹窗 |
| `system_dialog` | 系统权限弹窗 |
| `modal` | 业务弹窗 |
| `terms` | 协议正文详情页 |
| `onboarding` | 新手引导 |

### 主流程

| key | 标题 |
|-----|------|
| `search` | 搜索页 |
| `home` | 首页 / Feed |
| `detail` | 详情页 |
| `publish` | 发布 / 编辑 |
| `form` | 通用表单 |
| `payment` | 支付收银台 |
| `chat` | 聊天 / 消息 |
| `notification` | 通知中心 |
| `profile` | 个人中心 |
| `settings` | 设置页 |
| `webview` | H5 内嵌页 |
| `generic` | 兜底 |

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

1. `bootstrap_rules`：OCR 全文硬匹配（如系统权限、手机号登录屏）  
2. 读 `page_context.label` / `figma_best` + OCR 全文  
3. 按 YAML 顺序用 `label_patterns` / `screen_text_patterns` 匹配  
4. 首个命中者生效，否则 `generic`  
5. 定位步骤可经 `profile_key_for_login_step(label)` 覆盖为细粒度登录 profile  

## 与图谱 / Figma 的关系

`page_context_service` 已提供页面识别（骨架、Figma 文案）。Locate 模块 **消费** 其 `label`，不重复实现识别算法。

后续可将图谱节点 `metadata.page_type` 或 Figma frame 名直接映射为 profile key，见 [extending.md](./extending.md)。
