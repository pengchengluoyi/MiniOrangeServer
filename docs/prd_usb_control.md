# PRD v2：USB (ADB) 控制通道 —— 对齐 ClawNode 契约 + 设备指纹绑定

> 修订说明：v1 误以为 ADB 侧要从零新建执行器。实际项目**最早就是 ADB 方案**，且已存在按渠道划分的执行框架（`AdbExecutor` + `plugins/`）。v2 的本质是三件事：
> 1. **整改 ADB 下发链路**：按执行渠道划分，让 ADB 的命令格式/传参规则**对齐 ClawNode**，复用已有 adb 驱动/脚本。
> 2. **设备指纹绑定**：一个指纹ID 下绑定「一个 clawnode_id + 一个 adb_sn」，指向同一物理设备的两种连接方式；支持 ClawNode 动态 IP。
> 3. **EXEC_SCRIPT 兼容**：ADB 侧与 ClawNode 方案一致，但脚本另写一套，互不影响。
> 外加：USB adb `unauthorized` 授权提示。

---

## 1. 现状复盘（研究结论）

### 1.1 ADB 侧已有的东西
- **两套并行 ADB 路径**：
  - 旧：`driver/tentacle/engine/mobile/mAdb.py` `MAdbEngine`（引擎单例，u2 + `adb shell`，含解锁/黑屏处理）。
  - 新：`server/services/regression/executors/adb_executor.py` `AdbExecutor`（按 capability 分派，直接 `adb shell`，**这就是"按渠道划分"的雏形**）。
- **渠道执行框架已落地**：`plugins/executors/{adb,remote}.yaml`（executor 声明 provides 哪些抽象能力）+ `plugins/capabilities/*.yaml`（每能力多 implementations，按 `executor` 拆分 adb/remote 实现）+ 执行器注册表 `server/services/regression/executors/__init__.py`。
- **ADB 无脚本体系**：`EXEC_SCRIPT`/DSL/JS 目前**只在 ClawNode 侧**（`server/services/shared/clawnode_script.py` 的 `_BUILTIN`/`resolve_script`）。

### 1.2 ClawNode 命令契约（对齐目标，来自 `wClawNode.py:98` + `mRemote.py`）
标准帧 `{"type":"command","command":NAME,"params":{...},"trace_id":...}`；**坐标一律绝对像素 int，时长一律 `duration_ms` 毫秒 int**。

| 命令 | params | ADB 现状差异（要对齐的点） |
|---|---|---|
| `TAP` | `{x,y,duration_ms}` | `input tap` 不支持 duration_ms；长按需转 swipe |
| `SWIPE` | `{x,y,x2,y2,duration_ms}` | MAdbEngine 入参是归一化、AdbExecutor 是绝对 —— 需统一 |
| `KEY_EVENT` | `{keyevent}` 名字符串 | ADB 需数字 keycode；`paste` 无直接 keyevent |
| `OPEN_APP` | `{package,activity}` | ADB `monkey` 不支持 activity，带 activity 要用 `am start -n` |
| `CLOSE_APP`/`KILL_APP` | `{package}` | 基本一致（`am force-stop`） |
| `CLEAR_APP_CACHE` | `{package}` | ADB 有天然 `pm clear`（反而更简单） |
| `INPUT_TEXT` | `{text,x?,y?}` | 中文不支持（需 IME/clipboard 方案） |
| `SET_CLIPBOARD` | `{text}` | ADB 无原生，需 helper |
| `INSTALL_APK` | `{url,file_name?}` **URL** | ADB 是 `adb install {本地path}` —— **传参根本不同** |
| `RUN_SHELL` | `{command}` | 一致（ADB 全特权） |
| `EXEC_SCRIPT` | `{script,language,timeout_ms}` | ADB 侧**完全没有** |
| `GET_SCREENSHOT` | `{quality}`→base64 | ADB `exec-out screencap -p` 取二进制 |
| `WAKE_UP`/`GET_FOREGROUND_APP` | `{}` | 有对应实现 |

### 1.3 设备标识现状
- 主键 `sn`（`MDevice`），claw 设备 = `claw-<16hex>`，adb 设备 = adb serial。
- **无任何稳定跨连接指纹**：`android_id/serial/imei` 模型里都没有；`mac_address` 字段在但采集不保证 + 随机化。
- 去重靠 `_normalize_model(model)+ip_address` 的**脆弱启发式**（`device_service.py`）。
- `channels.adb.serial` 本应桥接 claw↔adb serial，但填充链路对 `claw-*` 是断的（`resolve_mobile_serial` 对 claw 原样返回）。
- **IP**：设备自报（非 socket 对端），只在 register/重连时更新，心跳不更新。

---

## 2. 设计目标与原则

1. **ClawNode 零回归**：不动 `wClawNode.py` 翻译层、`RemoteEngine`、`clawnode_script.py`、WS 帧协议、`direct_nodes` 语义。
2. **ADB 向 ClawNode 契约收敛**：ADB 下发的 command 名、params 字段、坐标/时长单位与 ClawNode **完全一致**；差异只在 executor 内部消化（如 URL→下载到本地再 install、name→keycode）。
3. **指纹为物理设备身份**：一个 `fingerprint_id` 绑定 `clawnode_id` + `adb_sn`，两种连接方式指向同一设备；设备列表以指纹为逻辑单位。
4. **复用已有 adb 驱动/脚本**：executor 整改优先接线现有 `AdbExecutor`/`MAdbEngine`，不重造轮子。

---

## 3. 方案一：ADB 下发链路整改（按渠道划分 + 对齐 ClawNode 契约）

### 3.1 统一命令契约层
定义**单一抽象命令契约**（就用 ClawNode 现有的命令名与 params schema，见 §1.2），两条渠道各自实现：

```
        抽象命令 {command, params}  ← 与 ClawNode 完全一致的契约
              │
     ┌────────┴─────────┐
  remote 渠道         adb 渠道
  wClawNode 翻译       AdbExecutor（整改后）
  → WS → App          → 本地 adb / MAdbEngine
```

- **remote 渠道**：现状不动。
- **adb 渠道**：整改 `AdbExecutor`，使其**入参 schema 与 ClawNode params 对齐**（收下 `{x,y,duration_ms}`、`{package,activity}`、`{url}` 等同名字段），在 executor 内部做渠道差异转换：
  - `INSTALL_APK`：收 `url` → executor 内部下载到临时文件 → `adb install`（对齐 ClawNode 的 URL 传参）。
  - `KEY_EVENT`：收 keyevent 名 → 内部 name→keycode 映射。
  - `OPEN_APP`：收 `{package,activity}`，有 activity 走 `am start -n`，无则 `monkey`。
  - `TAP`：收 `duration_ms`，>阈值转 `input swipe`(原地长按)。
  - `SWIPE`：统一收绝对像素（归一化换算收敛到入口处一次）。
  - `INPUT_TEXT`/`SET_CLIPBOARD`：中文经 IME/helper（P2）。

### 3.2 渠道选择器（消灭前缀硬编码）
新增 `resolve_control_channel(device) -> {"channel":"remote"|"adb", "adb_serial":...}`：
- 依据 §4 的指纹/通道状态判定，而非 `sn.startswith("claw-")`。
- 收敛现有散落判断：`driver/tentacle/manager.py:143` `apply_engine`、`driver/agent/Crawl/device_bootstrap.py:25` `_is_clawnode`、`device_manager._register_device` 前缀分支。
- **保留 `_ensure_engine_bound` 绑定校验**：claw 通道必须 `RemoteEngine`，adb 通道必须 `MAdbEngine`，防重构误配。

### 3.3 下发入口分叉
- **service/AI/回归执行链**：已天然支持——executor 注册表已按 `executor` 字段分派 adb/remote，只需让 `run_context` 的通道选择走 §3.2 解析器。
- **`/device/command` HTTP 直发**（`device_manager.send_command`）：
  - `channel==remote`：现状（WS + `_cmd_waiters` Future 等 ACTION_RESULT）。
  - `channel==adb`：走 `AdbExecutor` 本地同步执行，封装成与 ClawNode 相同的 `{status, ...}` 响应（本地调用即得结果，无需 trace_id/Future）。

---

## 4. 方案二：设备指纹 ID 与多连接绑定

### 4.1 数据模型
`MDevice` 新增字段（迁移方式与 `channels` 完全同构，`migration.py` 的 `schema_changes['m_device']` 追加）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `fingerprint_id` | TEXT, index | **物理设备稳定指纹**，跨连接唯一锚点 |
| `clawnode_id` | TEXT, nullable | 绑定的 ClawNode sn（`claw-xxx`），无则空 |
| `adb_sn` | TEXT, nullable | 绑定的 adb serial，无则空 |

> **模型形态决策见 §7 D1**：推荐"指纹为逻辑设备身份"——设备列表按 `fingerprint_id` 聚合为一个逻辑设备，其下挂 `channels.remote`(clawnode_id) 与 `channels.adb`(adb_sn) 两个连接槽。`sn` 主键保留以最小化迁移风险，但对外展示/操作以指纹为单位。

### 4.2 指纹算法（"不重复算法"）
指纹必须满足：**同一物理设备两种连接算出同一 ID；不同设备不碰撞；跨重连/换IP 稳定**。因此必须基于**硬件稳定量**，不能用 IP/model。

推荐算法（待你确认，§7 D2）：
```
raw = android_id            # adb: `settings get secure android_id`；ClawNode 需上报同一值
      [+ hardware_serial]   # 可选加盐：ro.serialno / Build.getSerial（有权限时）
fingerprint_id = "fp-" + sha256(raw)[:16]
```
- **adb 侧**：连接时 `adb -s <serial> shell settings get secure android_id` 直接拿到 → 算指纹。
- **remote 侧**：需 **ClawNode 客户端在 register/capabilities 帧上报 `android_id`**（当前未上报，见 §7 D3 外部依赖），server 用同算法算出同一指纹 → 与 adb 侧匹配即绑定。

### 4.3 绑定与合并流程
```
新连接到达（adb 发现 或 clawnode register）
  → 采集/上报 android_id → 算 fingerprint_id
  → 查库：该 fingerprint 是否已存在？
      ├─ 存在 → 把本连接写入对应槽（clawnode_id 或 adb_sn），更新 channels，不新建逻辑设备
      └─ 不存在 → 新建逻辑设备，写入指纹 + 对应槽
```
- 替换现有脆弱的 `model+ip` 去重：`dedupe_devices`/`remove_duplicate_hubs_for_claw` 改为**优先按 fingerprint 合并**，指纹缺失时才回退旧启发式（灰度过渡）。
- **手动绑定兜底**（§7 D3）：若 ClawNode 暂时无法上报 android_id，提供「手动把某 adb_sn 绑到某 clawnode 设备」的接口/界面，指纹置为手动关联。

### 4.4 动态 IP 支持
- **claw_id 作稳定锚点**：只要 ClawNode 客户端持久化 `claw-xxx`，换网段/重连 WiFi 仍是同一 sn，register 时刷新 `ip_address` —— 逻辑设备不变。
- **补齐心跳期 IP 更新**：ClawNode 心跳帧带上当前 IP（或周期性 re-register），`heartbeat`/`_update_device_status` 增加 IP 刷新（当前不更新）。
- **TCP-adb 跟随 IP**：若该设备走 `adb connect <ip>:5555`（transport=tcp），IP 变化后按最新 `ip_address` 重新 `adb connect`，并更新 `channels.adb.serial`。
- **指纹兜底 IP 漂移**：即便 IP 全变，靠 `fingerprint_id`（android_id 派生）仍能认出同一物理设备，不产生重复条目。

---

## 5. 方案三：EXEC_SCRIPT 兼容（ADB 与 ClawNode 一致，脚本另写）

- **协议一致**：ADB 侧 `EXEC_SCRIPT` 收与 ClawNode **相同的 params `{script, language, timeout_ms}` 和相同的 script_id 语义**，回传结构也一致（`{status, stdout, stderr}`），调用方无感知差异。
- **脚本另起一套，隔离 ClawNode**：新增 `server/services/shared/adb_script.py`（镜像 `clawnode_script.py` 的 `_BUILTIN`/`resolve_script`/`build_exec_script_command_params`/`parse_*` 接口），但：
  - `language`：ADB 侧支持 `dsl`（把 steps 映射成多条 `adb shell`）与 `shell`（直接 `adb shell` 脚本）；**不支持 ClawNode 的 `js`（`claw.*` API）** —— 遇到 js 脚本返回 `not_supported`（或提供等价 dsl 内建脚本）。
  - **内建脚本另写**：`open_settings`/`launch_package`/`open_app_settings`/`home` 等用 adb 原语重新实现（`am start -a android.settings.*`、`monkey`、`input keyevent 3` 等），**绝不 import `clawnode_script.py`**，保证改 adb 脚本不影响 clawnode。
- **分派**：`send_command`/executor 依据渠道选择 `clawnode_script` 或 `adb_script`，其余上层代码不变。

---

## 6. 方案四：USB adb 授权提示（unauthorized）

- `probe_adb` 已能识别 `unauthorized` 态；ADB 发现器发现 `adb devices` 里状态为 `unauthorized`/`offline` 的设备时：
  - 写 `channels.adb.state = "unauthorized"` + `reason`。
  - 该设备仍进设备列表，但标记为「⚠ 待授权」，前端提示：**「请在设备上勾选『允许 USB 调试』并信任此电脑」**。
  - `derive_main_status` 对 unauthorized 归为非 online（不可下发），授权成功后下一轮探测转 connected。

---

## 7. 待确认决策点（一起看可行性）

- **D1 指纹与 model 的关系（最关键）**：
  - 方案 A（推荐，低风险）：保留 `sn` 主键、一连接一行，新增 `fingerprint_id` 索引列做**聚合展示**，列表按指纹分组显示为一个逻辑设备。迁移小、回归风险低。
  - 方案 B（彻底）：以 `fingerprint_id` 为逻辑设备主体重构设备身份，`sn` 降级为连接属性。模型更干净但迁移/回归成本高。
  → 我倾向 **A**，先聚合展示，跑通后再评估是否要 B。
- **D2 指纹算法**：确认用 `sha256(android_id[+serial])[:16]`？还是你已有的算法（"我们的一个不重复算法"具体是什么，我按它实现）？
- **D3 ClawNode 上报 android_id（外部依赖）**：自动绑定要求 ClawNode 客户端上报 `android_id`（目前未上报）。能否改客户端？若短期不能，是否接受**手动绑定**作为过渡？
- **D4 双通道同时在线时默认下发渠道**：建议默认 `remote`（拟人化贴近真实用户），允许显式指定 `channel=adb`（adb 更快、全特权，适合装包/清缓存/取数据）。
- **D5 EXEC_SCRIPT 的 js 脚本**：ADB 侧遇到 ClawNode js 脚本，返回 `not_supported` 还是尽量提供等价 dsl 内建实现？建议先 `not_supported` + 常用几个给 dsl 等价。
- **D6 adb 发现范围**：只 USB，还是也纳入 `adb connect` 的 TCP 设备（`transport=tcp`，配合动态 IP）？建议都支持。

---

## 8. 改动清单

| # | 模块 | 改动 | ClawNode 风险 |
|---|---|---|---|
| 1 | `server/services/regression/executors/adb_executor.py` | 入参 schema 对齐 ClawNode params；补 KILL_APP/CLEAR_APP_CACHE/OPEN_APP(activity)/INSTALL_APK(url→下载)/EXEC_SCRIPT | 无（独立文件） |
| 2 | 新增 `server/services/shared/adb_script.py` | ADB 版脚本库（dsl/shell，内建脚本 adb 重写） | 无（不 import clawnode_script） |
| 3 | 新增 `resolve_control_channel()` | 渠道选择器，替换前缀硬编码 | 中（需回归引擎选择） |
| 4 | `driver/tentacle/manager.py`、`device_bootstrap.py` | 前缀判断→渠道选择器；保留绑定校验 | 中 |
| 5 | `server/models/mDevice.py` + `server/core/migration.py` | 加 `fingerprint_id/clawnode_id/adb_sn` 列 | 低 |
| 6 | 新增 ADB 发现器（轮询 `list_adb_serials` + 授权态） | 发现/注册/授权提示；写 `channels.adb` | 无 |
| 7 | `server/services/device_service.py` | 去重合并优先按 fingerprint，回退旧启发式 | 低（灰度） |
| 8 | `device_manager`：`register`/`heartbeat`/`_register_device` | 采集/存指纹、绑定槽、心跳期刷新 IP | 中 |
| 9 | `send_command` + `/device/command` | 按渠道分叉（adb 本地同步执行） | 低（remote 分支保持） |
| 10 | ClawNode 客户端（外部） | register/capabilities 上报 `android_id`（D3） | —（客户端侧） |
| 11 | 前端 | 指纹聚合展示、双通道徽标、待授权提示、下发渠道选择 | 无 |

---

## 9. 分期

- **P0 打通只读**：#5 模型 + #6 发现器（含授权提示）+ #3 渠道选择器 + adb 截图，USB 设备可见可截图。
- **P1 执行落地 + 对齐契约**：#1 adb_executor 契约对齐 + #4 引擎选择 + #9 下发分叉，USB 可点击/输入/装包。
- **P2 指纹绑定 + 脚本 + 动态IP**：#2 adb_script + #7/#8 指纹合并 + 动态 IP + #10 客户端上报 + #11 前端。

---

## 10. 验收标准

1. USB 设备插入 → 数秒出现在列表；未授权显示「⚠ 待授权」提示，授权后转 online。
2. ADB 下发的 command/params 与 ClawNode **同名同结构**，`/device/command` 对 adb 设备可点击/输入/启动/装包/EXEC_SCRIPT。
3. 同一物理设备经 ClawNode 与 USB 两种连接 → 列表显示**为一个逻辑设备**（指纹聚合），两连接槽状态独立。
4. ClawNode 换网段/重连 WiFi（IP 变）→ 仍是同一逻辑设备，不产生重复条目，IP 刷新。
5. **全程 ClawNode 注册/心跳/下发/投屏/EXEC_SCRIPT 行为零变化**（回归对比）。
