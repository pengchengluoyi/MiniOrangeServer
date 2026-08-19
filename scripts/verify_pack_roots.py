#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""S1b 验收：Pack 多根 + 优先级裁决。

四个根（高 → 低）：app > team > builtin > learned。核心要验的是：
  ① 同 kind+id 跨根冲突时高者胜，低者标 overridden_by 且**执行期不生效**；
  ② 学习产出（learned / doc / third_party）强制走人工确认，不会悄悄生效；
  ③ builtin 根不允许从 UI 写入；
  ④ 存量 63 条知识**不迁移**，config.json 保持原样。

用法：.venv/bin/python scripts/verify_pack_roots.py

脚本会在 <APP_DATA>/packs/ 下创建临时包，结束时清理并校验已还原。
退出码 0 = 全通过，1 = 有失败项。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from server.services.packs import ROOT_RANK, get_store  # noqa: E402
from server.services.packs.store import app_data_packs_root  # noqa: E402

_fails: list[str] = []
_c = TestClient(main.app)

TEAM_PACK = "verify-team-pack"
LEARNED_PACK = "verify-learned-pack"


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {got!r}" + ("" if ok else f"  期望 {want!r}"))
    if not ok:
        _fails.append(name)


def cleanup() -> None:
    base = app_data_packs_root()
    for p in (base / "team" / TEAM_PACK, base / "learned" / LEARNED_PACK,
              base / "apps" / "verify-app-id"):
        shutil.rmtree(p, ignore_errors=True)
    get_store().reload()


def create(root: str, pack_id: str, entry_id: str, raw: str = "", **kw) -> dict:
    body = {"kind": "recovery", "root": root, "pack_id": pack_id, "id": entry_id,
            "owner": "@verify", **kw}
    if raw:
        body["raw_yaml"] = raw
    return _c.post("/packs/create", json=body)


_OVERRIDE_YAML = """id: screen_asleep_or_locked
kind: recovery
title: 屏幕息屏（团队定制版）
enabled: true
owner: "@verify"
lifecycle: active
priority: 100
when: 团队定制：唤醒后多等一会儿
match:
  evidence:
    screen_blocked: "yes"
mode: deterministic
actions:
  - capability: wake_screen
  - capability: wait_ms
    params: {ms: 1200}
  - capability: dismiss_keyguard
verify:
  evidence:
    screen_blocked: "no"
"""


def test_roots_meta() -> None:
    print("\n[四个根：优先级与可写性]")
    d = _c.get("/packs/roots").json()["data"]
    ranks = {r["root"]: r["rank"] for r in d["roots"]}
    check("优先级顺序 app<team<builtin<learned",
          [ranks["app"], ranks["team"], ranks["builtin"], ranks["learned"]], [0, 1, 2, 3])
    check("与代码常量一致", ranks, dict(ROOT_RANK))
    writable = {r["root"]: r["writable"] for r in d["roots"]}
    check("builtin 不可写", writable["builtin"], False)
    check("其余三根可写", [writable["app"], writable["team"], writable["learned"]], [True, True, True])
    check("说明里写了优先级", d["precedence"], "app > team > builtin > learned")


def test_create_skeleton() -> None:
    print("\n[新建：骨架默认 draft，不会悄悄生效]")
    r = create("team", TEAM_PACK, "verify_ime_rule")
    check("创建成功", r.status_code, 200)
    item = r.json()["data"]["item"]
    check("落在 team 根", item["root"], "team")
    check("包名正确", item["pack_id"], TEAM_PACK)
    check("uid 形如 root/kind/id", item["uid"], "team/recovery/verify_ime_rule")
    check("默认 draft", item["lifecycle"], "draft")
    check("源文件已落盘", Path(item["source_path"]).is_file(), True)
    check("自动建了 pack.yaml",
          (app_data_packs_root() / "team" / TEAM_PACK / "pack.yaml").is_file(), True)

    active_ids = [o.id for o in get_store().active_objects("recovery")]
    check("draft 条目不参与执行", "verify_ime_rule" in active_ids, False)


def test_precedence() -> None:
    print("\n[优先级裁决：team 覆盖 builtin]")
    before = [o.title for o in get_store().active_objects("recovery")]
    check("覆盖前生效的是内置版", "屏幕息屏 / 锁屏" in before, True)

    r = create("team", TEAM_PACK, "screen_asleep_or_locked", raw=_OVERRIDE_YAML)
    check("覆盖条目创建成功", r.status_code, 200)

    rows = {x["uid"]: x for x in _c.get("/packs?kind=recovery").json()["data"]["items"]}
    check("内置那条被标记被覆盖",
          rows["builtin/recovery/screen_asleep_or_locked"]["overridden_by"],
          "team/recovery/screen_asleep_or_locked")
    check("team 那条自己不被覆盖",
          rows["team/recovery/screen_asleep_or_locked"]["overridden_by"], "")

    titles = [o.title for o in get_store().active_objects("recovery")]
    check("执行期生效的是团队版", "屏幕息屏（团队定制版）" in titles, True)
    check("内置版不再参与执行", "屏幕息屏 / 锁屏" in titles, False)
    check("同 id 只生效一条", sum(1 for o in get_store().active_objects("recovery")
                                if o.id == "screen_asleep_or_locked"), 1)

    # 真正的匹配链路也应该用团队版
    from server.services.regression import recovery as R
    hits = R.match_rules(R.Evidence(awake="no", locked="yes", screen_blocked="yes"), [])
    check("匹配到的规则动作是团队版（3 个动作）",
          len(hits[0].rule.actions) if hits else 0, 3)

    # 删掉覆盖 → 自动回落内置
    (app_data_packs_root() / "team" / TEAM_PACK / "entries" /
     "screen_asleep_or_locked.yaml").unlink()
    get_store().reload()
    titles2 = [o.title for o in get_store().active_objects("recovery")]
    check("删除覆盖后回落内置版", "屏幕息屏 / 锁屏" in titles2, True)


def test_learned_review_gate() -> None:
    print("\n[学习产出：强制人工确认]")
    r = create("learned", LEARNED_PACK, "verify_learned_rule")
    check("创建成功", r.status_code, 200)
    item = r.json()["data"]["item"]
    check("provider 自动置为 learned", item["provider"], "learned")
    check("强制 draft（未批准不生效）", item["lifecycle"], "draft")
    check("enabled=False", item["enabled"], False)

    # 即使把条目改成 active，未批准仍应被压回 draft
    path = Path(item["source_path"])
    path.write_text(path.read_text(encoding="utf-8").replace("lifecycle: draft", "lifecycle: active"),
                    encoding="utf-8")
    get_store().reload()
    entry = get_store().get_entry("learned/recovery/verify_learned_rule")
    check("手改 active 也被压回 draft", entry.obj.lifecycle if entry else None, "draft")
    check("仍不参与执行",
          "verify_learned_rule" in [o.id for o in get_store().active_objects("recovery")], False)

    manifest = app_data_packs_root() / "learned" / LEARNED_PACK / "pack.yaml"
    check("清单里 review.required=true", "required: true" in manifest.read_text(encoding="utf-8"), True)


def test_write_guards() -> None:
    print("\n[写入护栏]")
    check("builtin 根禁止写", create("builtin", "x", "y").status_code, 400)
    check("app 根缺 app_id", _c.post("/packs/create", json={
        "kind": "recovery", "root": "app", "id": "x"}).status_code, 400)
    check("未知根", create("nope", "x", "y").status_code, 400)
    check("重复 id", create("team", TEAM_PACK, "verify_ime_rule").status_code, 409)
    check("空 id", create("team", TEAM_PACK, "   ").status_code, 400)
    check("不支持的 kind", _c.post("/packs/create", json={
        "kind": "knowledge", "root": "team", "id": "x"}).status_code, 400)
    bad = _OVERRIDE_YAML.replace("- capability: wake_screen", "- capability: no_such_cap")
    check("引用不存在的能力", create("team", TEAM_PACK, "verify_bad_cap", raw=bad).status_code, 400)
    check("被拒的条目没落盘",
          (app_data_packs_root() / "team" / TEAM_PACK / "entries" / "verify_bad_cap.yaml").exists(),
          False)


def test_app_root_scope() -> None:
    print("\n[应用私有根：只对该应用生效]")
    APP = "verify-app-id"
    r = create("app", "app-pack", "verify_app_rule", app_id=APP)
    check("创建成功", r.status_code, 200)
    item = r.json()["data"]["item"]
    check("uid 含 app_id", item["uid"], f"app/{APP}/recovery/verify_app_rule")
    check("作用域记了 app_id", item["scope"]["app_ids"], [APP])

    ids_for_app = [e.id for e in get_store().list_entries(kind="recovery", app_id=APP)]
    check("查该应用能看到", "verify_app_rule" in ids_for_app, True)
    ids_other = [e.id for e in get_store().list_entries(kind="recovery", app_id="another-app")]
    check("查别的应用看不到", "verify_app_rule" in ids_other, False)


def test_bad_pack_reported() -> None:
    print("\n[坏包要报错而不是静默跳过]")
    base = app_data_packs_root() / "team" / TEAM_PACK
    # 缺 pack.yaml 的目录
    orphan = app_data_packs_root() / "team" / "verify-orphan"
    (orphan / "entries").mkdir(parents=True, exist_ok=True)
    (orphan / "entries" / "x.yaml").write_text("id: x\nkind: recovery\n", encoding="utf-8")
    # 坏 YAML 条目
    (base / "entries" / "verify_broken.yaml").write_text("a: [unclosed", encoding="utf-8")
    get_store().reload()
    msgs = " ".join(e.message for e in get_store().errors())
    check("缺 pack.yaml 被报出", "缺少 pack.yaml" in msgs, True)
    check("坏 YAML 被报出", "verify_broken" in " ".join(e.path for e in get_store().errors()), True)
    check("坏条目不影响好条目",
          "verify_ime_rule" in [e.id for e in get_store().list_entries(kind="recovery")], True)
    health = _c.get("/packs/health").json()["data"]
    check("健康接口把它们带给前端", health["error_count"] >= 2, True)
    shutil.rmtree(orphan, ignore_errors=True)
    (base / "entries" / "verify_broken.yaml").unlink(missing_ok=True)


def test_no_migration() -> None:
    print("\n[按约定不迁移存量知识]")
    import os

    cfg = Path(os.path.expanduser("~/Library/Application Support/MiniOrangeServer/config.json"))
    if not cfg.is_file():
        print("  ⚠️ 找不到 config.json，跳过")
        return
    data = json.loads(cfg.read_text(encoding="utf-8"))
    items = (data.get("testing") or {}).get("knowledge") or []
    check("config.json 里的知识条目仍在", len(items) >= 60, True)
    check("知识 kind 仍标未接入",
          _c.get("/packs/kinds").json()["data"]["kinds"][2]["ready"], False)
    packs_knowledge = _c.get("/packs?kind=knowledge").json()["data"]["total"]
    check("packs 里没有凭空多出知识条目", packs_knowledge, 0)


def main_() -> int:
    print("=== S1b 验收：Pack 多根与优先级裁决 ===")
    cleanup()
    try:
        test_roots_meta()
        test_create_skeleton()
        test_precedence()
        test_learned_review_gate()
        test_write_guards()
        test_app_root_scope()
        test_bad_pack_reported()
        test_no_migration()
    finally:
        print("\n[清理]")
        cleanup()
        left = [e.uid for e in get_store().list_entries() if e.root != "builtin"]
        check("临时包已清理干净", left, [])
        check("生效规则回到内置两条",
              sorted(o.id for o in get_store().active_objects("recovery")),
              ["screen_asleep_or_locked", "system_permission_dialog"])

    print("\n" + "=" * 46)
    if _fails:
        print(f"❌ {len(_fails)} 项失败：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
