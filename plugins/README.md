# MiniOrange Capability Plugins

这是 MiniOrange 自动化测试框架的 **能力插件目录**。所有自动化测试能力（点击 / 启动应用 / 清缓存 / 人工介入 / 视觉断言 / ...）都以 YAML 形式声明在这里，运行时由 `server/services/plugins/loader.py` 加载。

## 设计哲学

1. **AI 主导**：AI 大脑（LLM/VLM）是测试执行的主角；YAML 只声明"能做什么、用什么实现"，**不**写"什么时候做、按什么顺序做"。
2. **数据驱动**：增删能力、调整 `needs_vlm` 开关、改触发短语，都改 YAML 不改 Python，支持热更新。
3. **执行器抽象**：能力声明的是 **抽象 caps**（如 `system_pkg_clear`、`ui_native_input`），不是具体的 `adb` / `remote`。未来加 web/pc/iOS 只动 `executors/` 不动 `capabilities/`。
4. **运行时筛选**：plan 阶段 Router 按当前 connectivity（adb 通否 / remote 通否）过滤可用 implementations，再把瘦身后的菜单塞给 AI。

## 目录结构

```
plugins/
├── abstract_caps.yaml     # 所有抽象能力的全集（system_shell, ui_native_input, ...）
├── executors/             # 每个执行器声明自己实现哪些抽象能力
│   ├── adb.yaml
│   ├── remote.yaml        # ClawNode App on phone
│   ├── ai_persona.yaml    # AI 拟人化（多步 UI 操作）
│   ├── vlm.yaml           # 大模型视觉
│   ├── hitl.yaml          # 人工介入
│   └── *.yaml.disabled    # 未来执行器占位
└── capabilities/          # 每个 event_kind 一个 yaml
    ├── launch_app.yaml
    ├── tap_element.yaml
    ├── clear_app_cache.yaml
    ├── human_input_text.yaml
    └── ...
```

## Capability YAML Schema

```yaml
id: tap_element                       # 唯一 ID, snake_case
display_name: 点击元素                  # 中文显示名
event_kind: tap_element                # plan 阶段产出的事件类型（通常同 id）
category: ui_interaction               # 分类，决定 Skills 页分组
description: 在屏幕上点击指定元素
platforms: [android, ios, web]         # 适用平台
trigger_phrases: [点击, 点, 按, tap, click]  # AI 识别触发短语

needs_vlm: true                        # 默认 VLM 需求，可被 implementation 覆盖

implementations:                       # 执行路径列表，按 cost 升序
  - id: vlm_locate_remote_tap
    display_name: VLM 定位 + Remote 点击
    executor: remote                   # 关联 executors/remote.yaml
    requires_caps: [ui_native_input, ui_screenshot]
    needs_vlm: true
    locate_prompt: LOCATE_VISION       # 声明用的 prompt（后续 Step 3 实装）
    low_level:                         # 执行器原语调用形式
      command: TAP
      params: { x: "{x}", y: "{y}" }
    cost: 5                            # 同 capability 内按 cost 升序排
    description: VLM 给出坐标，ClawNode 发 TAP

ui:
  shown_in_settings: true              # 是否在 Skills 设置页展示
  examples: ["点击「同意」"]
```

## Executor YAML Schema

```yaml
id: adb
display_name: ADB (Server 端)
description: 通过 USB/TCP adb 直连设备，最特权
available_when: connectivity.adb       # 连通性条件，runtime 由 RunContext 提供
provides:                              # 实现的抽象 caps 列表
  - system_shell
  - system_pkg_clear
  - ui_native_input
  - ...
platforms: [android]
```

## 三种 connectivity 下菜单瘦身规则

| connectivity | 行为 |
|---|---|
| adb=T, remote=T | 完整菜单 |
| adb=T, remote=F | 砍 remote 独有实现，AI 全走 adb |
| adb=F, remote=T | 砍 adb 独有实现；`clear_cache` 自动落到 `ai_persona` 拟人路径 |
| adb=F, remote=F | 空菜单，PLAN_OVERVIEW 直接 decline |

## 修改流程（运营/QA）

1. 改 YAML（比如把 `needs_vlm: true` 改成 `false`）
2. 保存
3. 调 `POST /settings/skills/reload`（或等下次 mtime 检测）
4. Skills 页刷新 → 立即生效（无需发版）

## 添加新能力（开发）

1. 在 `capabilities/` 加新 yaml
2. 如果引入新抽象 cap，在 `abstract_caps.yaml` 声明
3. 在相关 `executors/*.yaml` 的 `provides:` 加上
4. 重启 / reload

## 添加新执行器（未来：web/pc/iOS）

1. 在 `executors/` 加新 yaml，声明 `provides:` 哪些抽象 caps
2. 实现对应的 runtime probe（连通性检测）
3. 不动任何 `capabilities/`——已有能力自动获得新执行路径
