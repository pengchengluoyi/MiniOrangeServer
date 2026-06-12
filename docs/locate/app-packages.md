# 主流应用包名注册表（App Packages）

- **资源包**：`server/resources/locate/app_packages.yaml`
- **加载模块**：`server/services/locate/app_packages.py`
- **环境变量**：`LOCATE_APP_PACKAGES_PATH`

## 作用

| 能力 | 说明 |
|------|------|
| 前台 App 识别 | 从 `engine.current_package()` / u2 `app_current` 解析包名 → 微信/QQ/小红书… |
| 页面 profile 增强 | 某 App 前台时，`page_supplements` 可把屏文映射到更细的 `phone_login` / `chat` 等 |
| 打开应用 | Copilot 按应用名解析包名时，DB 未命中则回退到本注册表 |
| 前置条件 | 如「已装微信」使用 `package_for_app_key("wechat")` |
| 回放展示 | `locate_debug` / `page_context` 含 `foreground_app_name`、`foreground_package` |

## 资源包字段

```yaml
apps:
  - key: wechat              # 内部标识
    name: 微信               # 中文名
    name_en: WeChat
    category: social         # 分类（展示用）
    android_packages:
      - com.tencent.mm
    ios_bundle_ids:
      - com.tencent.xin
    aliases:                 # 打开应用 / 别名匹配
      - 微信
      - wechat
    page_supplements:        # 可选：该 App 前台时的额外 profile 规则
      - profile: chat
        screen_text_patterns:
          - 通讯录
          - 朋友圈
```

## 已内置应用（节选）

国内：微信、QQ、企业微信、钉钉、飞书、小红书、抖音、快手、微博、B站、知乎、淘宝、京东、拼多多、支付宝、美团、高德、百度、造好物 …

国际：WhatsApp、Telegram、LINE、Facebook、Instagram、X(Twitter)、Messenger、TikTok、Gmail、Outlook、Teams、Zoom、YouTube、Spotify …

完整列表见 YAML 文件。

## API

```python
from server.services.locate.app_packages import (
    resolve_known_app_by_package,
    resolve_known_app_by_alias,
    package_for_app_key,
    get_foreground_package,
    list_known_apps,
)

app = resolve_known_app_by_package("com.tencent.mm")  # KnownApp(key=wechat, ...)
pkg = package_for_app_key("whatsapp")               # com.whatsapp
```

## 扩展

1. 在 YAML 末尾追加 `apps` 条目（包名务必小写存储，加载时会 normalize）
2. 或设置 `LOCATE_APP_PACKAGES_PATH` 指向团队维护的独立文件
3. 为高频 App 配置 `page_supplements`，在不改 `page_profiles.yaml` 的情况下提升该 App 内页面识别

## 与 page_profiles 的关系

- **app_packages**：认「哪个 App」（包名维度）
- **page_profiles**：认「什么类型的页面」（登录/首页/聊天…）

两者组合：前台为微信 + OCR 含「朋友圈」→ 可命中 `chat` profile（微信的 page_supplement）。
