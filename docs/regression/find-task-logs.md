# 用任务 ID 找日志

UI 上的任务号是 `task_id` 去掉 `cr-` 后的前 8 位。  
例如界面 `898b2038` → 库里 `cr-898b203890ac`。

数据不在 git 仓库，在本机 SQLite：

```
~/Library/Application Support/MiniOrangeServer/data/autobots.db
```

下文把该路径记为 `$DB`。

```bash
DB="$HOME/Library/Application Support/MiniOrangeServer/data/autobots.db"
SHORT=898b2038   # 换成界面上的 8 位
```

---

## 1. 用 8 位号找到完整 `run_id`

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode box
SELECT run_id, app_id, sn, platform, status, total, passed, failed, started_at, finished_at
FROM app_regression_runs
WHERE run_id LIKE '%${SHORT}%'
   OR substr(replace(run_id,'cr-',''),1,8)='${SHORT}';
SQL
```

记下完整 `run_id`（如 `cr-898b203890ac`），后面都用它。下文记为 `$RUN`。

---

## 2. 任务级数据（概况 + 每条用例摘要）

任务整包在 `app_regression_runs.payload`（JSON）。

```bash
python3 - <<'PY'
import sqlite3, json
DB = ".../autobots.db"
RUN = "cr-xxxxxxxx"

db = sqlite3.connect(DB)
row = db.execute("SELECT * FROM app_regression_runs WHERE run_id=?", (RUN,)).fetchone()
# 列：run_id, app_id, run_type, sn, platform, status, total, passed, failed, payload, started_at, finished_at
payload = json.loads(row[9]) if row[9] else {}
print(json.dumps(payload, ensure_ascii=False, indent=2)[:8000])

print("\n--- cases ---")
for c in payload.get("cases") or []:
    print(c.get("case_id"), c.get("status"), c.get("failure_category"),
          (c.get("summary") or "")[:200], "elapsed", c.get("elapsed_ms"))
PY
```

payload 里常用字段：`run_id` / `app_id` / `app_name` / `sn` / `model_name` / `provider_name` / `status` / `cases[]`。  
单条 case 摘要：`status`、`failure_category`、`summary`、`elapsed_ms`。

---

## 3. 每条用例的逐步日志

逐步动作在 `m_case_run_trace`，一行一条用例：

| 列 | 内容 |
|---|---|
| `run_id` | `{任务id}::{用例id}`，如 `cr-898b203890ac::CAM-GEN-010` |
| `batch_id` | 任务 id，等于 `cr-898b203890ac` |
| `overall_status` | pass / fail / partial / running |
| `report_payload` | 终态：`failure_category`、`decline_reason` |
| `event_results` | **逐步日志**（capability、status、thought、summary） |
| `plan_payload` | 目标一句话（agent 模式） |
| `run_context` | 设备 / 包名快照 |

列出该任务下所有用例：

```bash
sqlite3 "$DB" <<SQL
.headers on
.mode box
SELECT case_id, overall_status, total_events, passed, failed, skipped, elapsed_ms, started_at, finished_at
FROM m_case_run_trace
WHERE batch_id='$RUN'
ORDER BY started_at;
SQL
```

把每条用例的 `event_results` 全部打出来：

```bash
python3 - <<'PY'
import sqlite3, json
DB = ".../autobots.db"
RUN = "cr-xxxxxxxx"

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

for row in db.execute(
    "SELECT * FROM m_case_run_trace WHERE batch_id=? ORDER BY started_at", (RUN,)
):
    report = json.loads(row["report_payload"] or "{}")
    events = json.loads(row["event_results"] or "[]")
    print("=" * 80)
    print(row["case_id"], "overall=", row["overall_status"],
          "cat=", report.get("failure_category"),
          "events=", len(events))
    print("decline:", (report.get("decline_reason") or "")[:400])
    for i, e in enumerate(events, 1):
        print(f"  [{i:02d}] {e.get('status','')}  {e.get('capability_id','')}")
        print("       thought:", (e.get("ai_reasoning") or "")[:200])
        print("       summary:", (e.get("summary") or e.get("error") or "")[:200])
        if e.get("params"):
            print("       params:", json.dumps(e["params"], ensure_ascii=False)[:200])
PY
```

一条用例的原始 JSON（含截图 thumb 会很大）：

```bash
python3 - <<'PY'
import sqlite3, json
db = sqlite3.connect(".../autobots.db")
row = db.execute(
    "SELECT event_results FROM m_case_run_trace WHERE run_id=?",
    ("cr-xxxxxxxx::CAM-GEN-010",),
).fetchone()
events = json.loads(row[0] or "[]")
# 去掉 thumb 再打印
for e in events:
    e.pop("thumb", None)
print(json.dumps(events, ensure_ascii=False, indent=2))
PY
```

---

## 4. 用例原文（飞书缓存）

步骤 / 前置 / 预期在 `apps.env` 的 `feishu_cases_cache`，不在 trace 里。

```bash
python3 - <<'PY'
import sqlite3, json
DB = ".../autobots.db"
APP_ID = "b5431352-e34a-4d53-9e5b-33d5b130f0ff"  # 来自 app_regression_runs.app_id
WANT = {"CAM-GEN-010", "CAM-VIEW-007"}           # 要看的编号

db = sqlite3.connect(DB)
env = json.loads(db.execute("SELECT env FROM apps WHERE id=?", (APP_ID,)).fetchone()[0])
for c in env["feishu_cases_cache"]["cases"]:
    if c.get("case_id") not in WANT:
        continue
    print(c["case_id"], c.get("name"))
    print("  pre :", c.get("precondition"))
    print("  steps:", c.get("steps_raw"))
    print("  exp :", c.get("expected_raw"))
PY
```

---

## 数据对应关系

```
UI 8 位  ──►  app_regression_runs.run_id = cr-xxxxxxxx...
                    │
                    │  payload.cases[]          任务概况、每条摘要
                    │
                    └──►  m_case_run_trace.batch_id = 同一个 cr-...
                              run_id = cr-...::CAM-XXX
                              event_results[]       逐步动作
                              report_payload        失败分类 / 结束原因
```
