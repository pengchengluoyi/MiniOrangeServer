# PRD v3：USB (ADB) 控制通道 —— 对齐 ClawNode 契约 + 设备指纹绑定（决策定稿）

> 相对 v2 的变化：§7 的六个决策点已全部拍板并固化到方案中。核心结论：
> 1. **D1 方案A**：ClawNode 连接后**上报设备真实 SN** 并入库；若已存在相同真实 SN 的 adb 连接设备，直接把 ClawNode 连接**合并到同一 `fingerprint_id`**。
> 2. **D2 指纹按平台取唯一标识**：安卓用 **SN（`ro.serialno`）**、iOS 用 **DID（UDID）**，不再写死 android_id。
> 3. **D3 改 ClawNode 连接传参接口**上报该唯一标识（无外部阻塞）。
> 4. **D4 渠道选择交给已有大模型能力**，双通道在线时**尽量优先 adb**。
> 5. **D5 EXEC_SCRIPT 的 js 脚本**：ADB 侧返回 `not_supported`。
> 6. **D6 新增 `adb connect` TCP 发现渠道**（与 ClawNode 连接方式一致），连接建立后**取设备 SN 作为区分标识**。

---

## 0. 一句话目标

在**完全不影响现有 ClawNode App 控制方案**的前提下，把项目最早的 ADB 方案整改为「按执行渠道划分、命令契约对齐 ClawNode」的下发链路，并通过**设备真实 SN/DID 派生的指纹**把同一物理设备的 ClawNode 连接与 adb（USB/TCP）连接绑定为一个逻辑设备。

---

## 1. 现状复盘（研究结论）

### 1.1 ADB 侧已有的东西
- **两套并行 ADB 路径**：
  - 旧：`driver/tentacle/engine/mobile/mAdb.py` `MAdbEngine`（引擎单例，u2 + `adb shell`，含解锁/黑屏处理）。
  - 新：`server/services/regression/executors/adb_executor.py` `AdbExecutor`（按 capability 分派，直接 `adb shell`，**这就是"按渠道划分"的雏形**）。
- **渠道执行框架已落地**：`plugins/executors/{adb,remote}.yaml`（executor 声明 provides 哪些抽象能力）+ `plugins/capabilities/*.yaml`（每能力多 implementations，按 `executor` 拆分 adb/remote 实现，按 cost 排序）+ 执行器注册表 `server/services/regression/executors/__init__.py`。
- **大模型能力选择已存在**：`server/services/plugins/registry.py` 的 `filter_capabilities_by_connectivity()` 会按连通性（adb/remote/vlm/hitl）过滤出可用 implementations 交给 AI plan（`server/services/ai/plan/prompt.py`），implementations 已按 cost 排序 —— **D4 的渠道选择直接复用这套**。
- **ADB 无脚本体系**：`EXEC_SCRIPT`/DSL/JS 目前**只在 ClawNode 侧**（`server/services/shared/clawnode_script.py` 的 `_BUILTIN`/`resolve_script`）。

### 1.2 ClawNode 命令契约（对齐目标，来自 `wClawNode.py:98` + `mRemote.py`）
标准帧 `{"type":"command","command":NAME,"params":{...},"trace_id":...}`；**坐标一律绝对像素 int，时长一律 `duration_ms` 毫秒 int**。

| 命令 | params | ADB 现状差异（要对齐的点） |
|---|---|---|
| `TAP` | `{x,y,duration_ms}` | `input tap` 不支持 duration_ms；长按需转 swipe |
| `SWIPE` | `{x,y,x2,y2,duration_ms}` | MAdbEngine 入参归一化、AdbExecutor 绝对 —— 需统一 |
| `KEY_EVENT` | `{keyevent}` 名字符串 | ADB 需数字 keycode；`paste` 无直接 keyevent |
| `OPEN_APP` | `{package,activity}` | ADB `monkey` 不支持 activity，带 activity 用 `am start -n` |
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
- 主键 `sn`（`MDevice`），claw 连接 = `claw-<16hex>`，adb 连接 = adb serial。**注意这是"连接标识"，不是"设备真实 SN"**。
- **无任何稳定跨连接指纹**：`android_id/serial/imei` 模型里都没有；`mac_address` 采集不保证 + 随机化。
- 去重靠 `_normalize_model(model)+ip_address` 的**脆弱启发式**（`device_service.py`）。
- **IP**：设备自报（非 socket 对端），只在 register/重连时更新，心跳不更新。

---

## 2. 关键概念：三层标识（v3 定稿）

| 层级 | 名称 | 取值 | 作用 |
|---|---|---|---|
| 物理设备 | `fingerprint_id` | 由平台唯一标识派生（见 §4.2） | **逻辑设备身份**，跨连接合并的锚点 |
| 硬件唯一量 | `hw_uid` | 安卓=真实 SN(`ro.serialno`)；iOS=DID(UDID) | 指纹原料 + **合并键**（同 hw_uid 即同一台设备） |
| 连接句柄 | `sn`（现有主键） | ClawNode=`claw-xxx`；adb=adb serial(usb) 或 `ip:port`(tcp) | 一条具体连接，挂在某 `fingerprint_id` 下 |

> 一句话：**`hw_uid` 相同 → 同 `fingerprint_id` → 一个逻辑设备**，其下可挂 `clawnode_id` 和 `adb_sn` 两个连接句柄。

---

## 3. 方案一：ADB 下发链路整改（按渠道划分 + 对齐 ClawNode 契约）

### 3.1 统一命令契约层
以 ClawNode 现有命令名与 params schema（§1.2）为**单一抽象契约**，两渠道各自实现：

```
        抽象命令 {command, params}  ← 与 ClawNode 完全一致的契约
              │
     ┌────────┴─────────┐
  remote 渠道         adb 渠道
  wClawNode 翻译       AdbExecutor（整改后）
  → WS → App          → 本地 adb / MAdbEngine
```

- **remote 渠道**：现状不动。
- **adb 渠道**：整改 `AdbExecutor`，入参 schema 与 ClawNode params 对齐（同名字段），渠道差异在 executor 内部消化：
  - `INSTALL_APK`：收 `url` → executor 内部下载到临时文件 → `adb install`。
  - `KEY_EVENT`：收 keyevent 名 → 内部 name→keycode 映射。
  - `OPEN_APP`：收 `{package,activity}`，有 activity 走 `am start -n`，无则 `monkey`。
  - `TAP`：收 `duration_ms`，>阈值转 `input swipe`（原地长按）。
  - `SWIPE`：统一收绝对像素（归一化换算收敛到入口一次）。
  - `INPUT_TEXT`/`SET_CLIPBOARD`：中文经 IME/helper（P2）。

### 3.2 渠道选择器（消灭前缀硬编码）
新增 `resolve_control_channel(device) -> {"channel":"remote"|"adb", "adb_serial":...}`：
- 依据指纹下挂的连接槽 + `channels` 通道状态判定，而非 `sn.startswith("claw-")`。
- 收敛现有散落判断：`driver/tentacle/manager.py:143` `apply_engine`、`driver/agent/Crawl/device_bootstrap.py:25` `_is_clawnode`、`device_manager._register_device` 前缀分支。
- **保留 `_ensure_engine_bound` 绑定校验**：claw 通道必须 `RemoteEngine`，adb 通道必须 `MAdbEngine`。

### 3.3 下发入口分叉
- **service/AI/回归执行链**：executor 注册表已按 `executor` 字段分派 adb/remote，只需让 `run_context` 的通道选择走 §3.2 解析器（AI 场景的渠道选择见 §6/D4）。
- **`/device/command` HTTP 直发**（`device_manager.send_command`）：
  - `channel==remote`：现状（WS + `_cmd_waiters` Future 等 ACTION_RESULT）。
  - `channel==adb`：走 `AdbExecutor` 本地同步执行，封装成与 ClawNode 相同的 `{status,...}` 响应。

---

## 4. 方案二：设备指纹与多连接绑定（D1 + D2 定稿）

### 4.1 数据模型
`MDevice` 新增字段（迁移方式与 `channels` 同构，`migration.py` 的 `schema_changes['m_device']` 追加）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `fingerprint_id` | TEXT, index | 物理设备稳定指纹，跨连接合并锚点 |
| `hw_uid` | TEXT, index | 设备真实唯一标识（安卓 SN / iOS DID），合并键 |
| `clawnode_id` | TEXT, nullable | 绑定的 ClawNode 连接句柄（`claw-xxx`） |
| `adb_sn` | TEXT, nullable | 绑定的 adb 连接句柄（serial 或 `ip:port`） |

> **模型形态（D1=方案A）**：保留 `sn` 主键、一连接一行；`fingerprint_id` 做**聚合展示**——设备列表按 `fingerprint_id` 分组显示为一个逻辑设备，其下挂 `channels.remote`(clawnode) 与 `channels.adb`(adb) 两个连接槽。迁移小、对 ClawNode 零回归。

### 4.2 指纹算法（D2 定稿：按平台取唯一标识）
```
# 每条连接建立后先拿到平台唯一量 hw_uid
android:  hw_uid = ro.serialno           # 真实设备 SN
ios:      hw_uid = UDID (DID)

# 指纹派生（平台加前缀防跨平台碰撞，hash 统一长度）
fingerprint_id = "fp-" + platform + "-" + sha256(hw_uid)[:16]
```
- **合并键就是 `hw_uid`**（同真实 SN/DID 即同一物理设备），`fingerprint_id` 是它的稳定派生值。
- 不再依赖 android_id；不同平台用各自的唯一标识。
- （待定的仅是"是否对 `hw_uid` 做 sha256"这个细节——若你更想直接用 SN 明文当 `fingerprint_id` 也可，见 §8 备注，不阻塞实现。）

### 4.3 各连接如何取 `hw_uid`（D3 定稿）
- **adb（USB/TCP）**：连接建立后 `adb -s <serial> shell getprop ro.serialno`（iOS 走对应 UDID 获取）。
- **ClawNode**：**改 ClawNode App 连接传参接口**，在 register/capabilities 帧新增上报 `hw_uid`（安卓上报 SN、iOS 上报 DID）。server 端在 `handle_clawnode_register` 读取并入库。

### 4.4 绑定与合并流程（D1 定稿）
```
新连接到达（adb 发现 或 clawnode register）
  → 取/上报 hw_uid → 算 fingerprint_id
  → 查库：是否已有相同 hw_uid（=相同真实 SN/DID）的逻辑设备？
      ├─ 有 → 把本连接写入对应槽（clawnode_id 或 adb_sn），并入该 fingerprint_id，不新建逻辑设备
      └─ 无 → 新建逻辑设备，写 fingerprint_id + hw_uid + 对应槽
```
- 具体到你说的场景：**ClawNode 连接上报真实 SN → 若已存在相同 SN 的 adb 设备 → 直接把 ClawNode 合并到该 `fingerprint_id`**（反向亦然）。
- 替换脆弱的 `model+ip` 去重：`dedupe_devices`/`remove_duplicate_hubs_for_claw` 改为**优先按 `hw_uid`/`fingerprint_id` 合并**，缺失时回退旧启发式（灰度过渡）。

### 4.5 动态 IP 支持
- **claw_id 作稳定连接锚点**：ClawNode 客户端持久化 `claw-xxx`，换网段/重连 WiFi 仍是同一连接句柄，register 刷新 `ip_address`。
- **补齐心跳期 IP 更新**：心跳帧带当前 IP（或周期性 re-register），`heartbeat`/`_update_device_status` 增加 IP 刷新（当前不更新）。
- **TCP-adb 跟随 IP**：走 `adb connect <ip>:5555`（transport=tcp）的设备，IP 变化后按最新 `ip_address` 重新 `adb connect`，更新 `channels.adb.serial`。
- **指纹兜底 IP 漂移**：IP 全变也靠 `hw_uid` 认出同一物理设备，不产生重复条目。

---

## 5. 方案三：EXEC_SCRIPT 兼容（D5 定稿：js 返回 not_supported）

- **协议一致**：ADB 侧 `EXEC_SCRIPT` 收与 ClawNode 相同的 params `{script,language,timeout_ms}` 与 script_id 语义，回传结构一致（`{status,stdout,stderr}`），上层无感知。
- **脚本另起一套，隔离 ClawNode**：新增 `server/services/shared/adb_script.py`（镜像 `clawnode_script.py` 的 `_BUILTIN`/`resolve_script`/`build_exec_script_command_params`/`parse_*` 接口），但：
  - `language`：支持 `dsl`（steps → 多条 `adb shell`）与 `shell`（直接 `adb shell` 脚本）。
  - **`js`（ClawNode `claw.*` API）→ 直接返回 `not_supported`**（D5）。
  - 内建脚本用 adb 原语**重新实现**（`am start -a android.settings.*`、`monkey`、`input keyevent 3` 等），**绝不 import `clawnode_script.py`**，改 adb 脚本不影响 clawnode。
- **分派**：`send_command`/executor 依渠道选择 `clawnode_script` 或 `adb_script`。

---

## 6. 方案四：双通道渠道选择（D4 定稿：交给大模型，优先 adb）

- **AI 执行场景**：复用已有的 `filter_capabilities_by_connectivity()` + AI plan 菜单机制。当一台逻辑设备 remote+adb 双通道都连通时，把**两渠道的 implementations 都放进能力菜单**交给大模型选择；通过 **cost/priority 排序让 adb 实现排在前面**（adb 更快、全特权），引导模型优先选 adb。
- **非 AI 直发场景**（`/device/command` 未显式指定渠道）：默认 `adb`（可用时），否则回落 `remote`；允许显式 `channel` 覆盖。
- 结论：能 adb 就 adb，特殊能力（拟人化、无 adb 权限项）再由模型/回落走 remote。

---

## 7. 方案五：设备发现与授权提示（D6 定稿：新增 TCP adb 发现）

### 7.1 两种 adb 发现渠道
- **USB adb 发现**：后台轮询 `connectivity_probe.list_adb_serials()`（已实现）拿在线 serial，新增/消失做注册/下线。
- **TCP adb 发现（D6 新增，与 ClawNode 连接方式一致）**：支持 `adb connect <ip>:<port>` 建立连接（可由配置/发现服务/设备上报 IP 触发），连接后同样 `getprop ro.serialno` **取设备真实 SN 作区分标识** → 算指纹 → 合并。`channels.adb.transport=tcp`。
- 每轮结束触发 `notify_device_list_changed()`（与 ClawNode 复用同一广播）。发现器与 ClawNode 的 `monitor_heartbeats()` 并列，互不干扰。

### 7.2 授权提示（unauthorized）
- `probe_adb` 已识别 `unauthorized`；发现 `adb devices`/`adb connect` 结果为 `unauthorized`/`offline`：
  - 写 `channels.adb.state="unauthorized"` + `reason`。
  - 设备仍进列表，标记「⚠ 待授权」，前端提示：**「请在设备上勾选『允许 USB 调试』并信任此电脑」**。
  - `derive_main_status` 对 unauthorized 归为非 online（不可下发），授权成功后下一轮探测转 connected。

---

## 8. 改动清单

| # | 模块 | 改动 | ClawNode 风险 |
|---|---|---|---|
| 1 | `server/services/regression/executors/adb_executor.py` | 入参对齐 ClawNode params；补 KILL_APP/CLEAR_APP_CACHE/OPEN_APP(activity)/INSTALL_APK(url→下载)/EXEC_SCRIPT | 无（独立文件） |
| 2 | 新增 `server/services/shared/adb_script.py` | ADB 版脚本库（dsl/shell；js→not_supported；内建脚本 adb 重写） | 无（不 import clawnode_script） |
| 3 | 新增 `resolve_control_channel()` | 渠道选择器，替换前缀硬编码 | 中（回归引擎选择） |
| 4 | `driver/tentacle/manager.py`、`device_bootstrap.py` | 前缀判断→渠道选择器；保留绑定校验 | 中 |
| 5 | `server/models/mDevice.py` + `server/core/migration.py` | 加 `fingerprint_id/hw_uid/clawnode_id/adb_sn` 列 | 低 |
| 6 | 新增 ADB 发现器（USB 轮询 + **TCP `adb connect`** + 取 SN + 授权态） | 发现/注册/合并/授权提示；写 `channels.adb` | 无 |
| 7 | `server/services/device_service.py` | 去重合并优先按 `hw_uid`/fingerprint，回退旧启发式 | 低（灰度） |
| 8 | `device_manager`：`register`/`register_clawnode`/`heartbeat`/`_register_device` | 读/存 hw_uid、算指纹、合并槽、心跳期刷新 IP | 中 |
| 9 | `send_command` + `/device/command` | 按渠道分叉（adb 本地同步执行），默认 adb 优先 | 低（remote 分支保持） |
| 10 | AI plan / `registry.filter_capabilities_by_connectivity` | 双通道时两渠道 implementations 入菜单，cost 排序偏向 adb | 低 |
| 11 | **ClawNode 客户端（外部，D3）** | register/capabilities 传参接口新增上报 `hw_uid`（安卓 SN / iOS DID） | —（客户端侧） |
| 12 | `handle_clawnode_register`（`wClawNode.py`） | 读取上报的 `hw_uid` 并入库 | 低 |
| 13 | 前端 | 指纹聚合展示、双通道徽标、待授权提示、（可选）渠道显示 | 无 |

---

## 9. 分期

- **P0 打通只读 + 发现**：#5 模型 + #6 发现器（USB+TCP+授权提示）+ #3 渠道选择器 + adb 截图，USB/TCP 设备可见可截图。
- **P1 执行落地 + 契约对齐**：#1 adb_executor 对齐 + #4 引擎选择 + #9 下发分叉，adb 设备可点击/输入/装包。
- **P2 指纹绑定 + 脚本 + 动态IP + 渠道选择**：#2 adb_script + #7/#8/#11/#12 指纹合并（含 ClawNode 上报 SN）+ 动态 IP + #10 大模型渠道选择 + #13 前端。

---

## 10. 验收标准

1. USB/TCP adb 设备接入 → 数秒出现在列表；未授权显示「⚠ 待授权」，授权后转 online。
2. ADB 下发的 command/params 与 ClawNode **同名同结构**，`/device/command` 对 adb 设备可点击/输入/启动/装包/EXEC_SCRIPT（js 脚本明确返回 not_supported）。
3. 同一物理设备经 ClawNode 与 adb 两种连接（真实 SN 相同）→ 列表显示**为一个逻辑设备**（指纹聚合），两连接槽状态独立；先有 adb 后上 ClawNode 会自动合并到同一 `fingerprint_id`。
4. ClawNode 换网段/重连 WiFi（IP 变）→ 仍是同一逻辑设备，不产生重复条目，IP 刷新。
5. AI 执行时双通道设备优先走 adb（模型可选），特殊能力回落 remote。
6. **全程 ClawNode 注册/心跳/下发/投屏/EXEC_SCRIPT 行为零变化**（回归对比）。

---

## 附：备注（不阻塞实现的小项）
- 指纹是否对 `hw_uid` 做 sha256：默认做（统一长度/脱敏）；若倾向明文 SN 直接当 `fingerprint_id` 也可，二选一在实现时定。
- iOS DID 获取通道（`MAdbEngine` 不管 iOS，`IOSEngine` 侧）在 P2 iOS 支持时细化，本期以安卓 SN 为主。
