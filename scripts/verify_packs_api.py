#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""Packs 控制台 API 验收脚本。

覆盖前端控制台依赖的全部读写契约（见 docs/plan-skill-packs-and-console.md §5/§7）：
  列表/分类/详情 → 筛选 → 启停写回 YAML → 整份保存的校验拦截 → dry-run 预演

用法：
    .venv/bin/python scripts/verify_packs_api.py            # 不碰设备
    .venv/bin/python scripts/verify_packs_api.py 5fda2f6d   # 追加真机 dry-run

写入类用例会改 plugins/recovery/*.yaml，跑完自动还原并校验内容一致。
退出码 0 = 全通过，1 = 有失败项。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

_fails: list[str] = []
_c = TestClient(main.app)

RULE_UID = "builtin/recovery/screen_asleep_or_locked"
_ROOT = Path(__file__).resolve().parents[1]  # MiniOrangeServer/
RULE_PATH = _ROOT / "plugins/recovery/screen_asleep_or_locked.yaml"


def _short(v, limit: int = 88) -> str:
    text = repr(v)
    return text if len(text) <= limit else text[:limit] + f"…({len(text)} 字)"


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {_short(got)}" + ("" if ok else f"  期望 {_short(want)}"))
    if not ok:
        _fails.append(name)


def test_kinds() -> None:
    print("\n[分类元数据：前端 Tab 用]")
    d = _c.get("/packs/kinds").json()["data"]
    kinds = {k["kind"]: k for k in d["kinds"]}
    check("四类都在", sorted(kinds.keys()), ["capability", "knowledge", "oracle", "recovery"])
    check("能力已就绪", kinds["capability"]["ready"], True)
    check("恢复已就绪", kinds["recovery"]["ready"], True)
    check("知识已就绪（只读镜像）", kinds["knowledge"]["ready"], True)
    check("知识不带 not_ready_reason", bool(kinds["knowledge"]["not_ready_reason"]), False)
    check("恢复条目数 ≥2", kinds["recovery"]["count"] >= 2, True)


def test_list_and_filters() -> None:
    print("\n[列表与筛选]")
    d = _c.get("/packs?kind=recovery").json()["data"]
    row = next((r for r in d["items"] if r["id"] == "screen_asleep_or_locked"), None)
    check("能取到息屏规则", row is not None, True)
    if row is None:
        return
    # 前端列表每行必须有这些字段（谁提供、谁负责、作用域、统计、状态）
    for field in ("uid", "kind", "id", "title", "provider", "owner", "lifecycle",
                  "root", "scope", "stats", "source_path", "when", "summary", "enabled"):
        check(f"行含 {field}", field in row, True)
    check("provider 正确", row["provider"], "device_team")
    check("owner 非空", bool(row["owner"]), True)

    check("按 provider 筛", all(r["provider"] == "device_team"
                              for r in _c.get("/packs?provider=device_team").json()["data"]["items"]), True)
    check("按 lifecycle 筛", all(r["lifecycle"] == "active"
                               for r in _c.get("/packs?lifecycle=active").json()["data"]["items"]), True)
    check("关键词命中", _c.get("/packs?q=锁屏").json()["data"]["total"] >= 1, True)
    check("关键词不命中", _c.get("/packs?q=绝不存在的词xyz").json()["data"]["total"], 0)
    check("非法 kind → 400", _c.get("/packs?kind=nope").status_code, 400)

    print("\n[能力：纯声明式标记必须诚实]")
    caps = _c.get("/packs?kind=capability").json()["data"]["items"]
    by_id = {c["id"]: c for c in caps}
    for cid in ("probe_device_state", "wake_screen", "dismiss_keyguard"):
        check(f"{cid} 标纯声明式", by_id[cid]["detail"]["pure_declarative"], True)
    check("tap_element 有 Python 分支", by_id["tap_element"]["detail"]["has_python_branch"], True)
    check("tap_element 不算纯声明式", by_id["tap_element"]["detail"]["pure_declarative"], False)
    check("系统专用能力标出可见域", by_id["probe_device_state"]["scope"]["visible_to"], ["system"])


def test_detail() -> None:
    print("\n[详情]")
    d = _c.get(f"/packs/{RULE_UID}").json()["data"]["item"]
    check("含原始 YAML", len(d.get("raw_yaml", "")) > 100, True)
    check("mode 正确", d["detail"]["mode"], "deterministic")
    check("动作列表完整", [a["capability"] for a in d["detail"]["actions"]],
          ["wake_screen", "wait_ms", "dismiss_keyguard", "wait_ms"])
    check("复查条件在", d["detail"]["verify"]["evidence"], {"awake": "yes", "locked": "no"})
    check("护栏在", "清除数据" in d["detail"]["forbid"]["text_any"], True)
    check("依据笔记在", len(d["detail"]["evidence_notes"]) >= 1, True)
    check("不存在 uid → 404", _c.get("/packs/builtin/recovery/nope").status_code, 404)
    check("uid 格式错 → 400", _c.get("/packs/onlyone").status_code, 400)


def test_lifecycle_roundtrip() -> None:
    print("\n[启停：写回 YAML 且不破坏注释]")
    before = RULE_PATH.read_text(encoding="utf-8")
    r = _c.post(f"/packs/{RULE_UID}/lifecycle", json={"lifecycle": "deprecated", "enabled": False})
    check("停用成功", r.status_code, 200)
    check("停用后状态", r.json()["data"]["item"]["lifecycle"], "deprecated")
    check("停用后 enabled", r.json()["data"]["item"]["enabled"], False)
    # 停用后不该再出现在「生效中」筛选里
    active_ids = [x["id"] for x in _c.get("/packs?kind=recovery&lifecycle=active").json()["data"]["items"]]
    check("停用后不在生效列表", "screen_asleep_or_locked" in active_ids, False)

    r = _c.post(f"/packs/{RULE_UID}/lifecycle", json={"lifecycle": "active", "enabled": True})
    check("重新启用", r.json()["data"]["item"]["lifecycle"], "active")
    after = RULE_PATH.read_text(encoding="utf-8")
    check("文件内容还原一致", after.strip() == before.strip(), True)
    check("注释未丢", "# 屏幕不亮时其它规则都没意义" in after, True)
    check("非法 lifecycle → 400",
          _c.post(f"/packs/{RULE_UID}/lifecycle", json={"lifecycle": "whatever"}).status_code, 400)


def test_save_validation() -> None:
    print("\n[整份保存：校验不过绝不落盘]")
    original = RULE_PATH.read_text(encoding="utf-8")

    cases = [
        ("坏 YAML", "a: [unclosed"),
        ("顶层不是对象", "- just\n- a\n- list\n"),
        ("非法 mode", original.replace("mode: deterministic", "mode: nonsense")),
        ("引用不存在的能力", original.replace("- capability: wake_screen", "- capability: no_such_cap")),
        ("擅自改 id", original.replace("id: screen_asleep_or_locked", "id: renamed")),
        ("deterministic 但删掉 actions",
         original.split("actions:")[0] + "verify:\n  evidence:\n    awake: \"yes\"\n"),
    ]
    for name, payload in cases:
        code = _c.put(f"/packs/{RULE_UID}", json={"raw_yaml": payload}).status_code
        check(f"{name} → 400", code, 400)
        check(f"{name} 未写坏文件", RULE_PATH.read_text(encoding="utf-8") == original, True)

    r = _c.put(f"/packs/{RULE_UID}", json={"raw_yaml": original})
    check("合法保存 → 200", r.status_code, 200)
    check("保存后文件一致", RULE_PATH.read_text(encoding="utf-8") == original, True)


def test_dry_run_errors() -> None:
    print("\n[dry-run 参数校验]")
    check("缺 sn → 400", _c.post(f"/packs/{RULE_UID}/dry-run").status_code, 400)
    check("不支持的 source → 400",
          _c.post(f"/packs/{RULE_UID}/dry-run?sn=x&source=sample").status_code, 400)
    check("capability 不支持 dry-run → 400",
          _c.post("/packs/builtin/capability/tap_element/dry-run?sn=x").status_code, 400)
    check("设备不在线 → 409",
          _c.post(f"/packs/{RULE_UID}/dry-run?sn=nosuchdevice").status_code, 409)


def test_reload() -> None:
    print("\n[重载]")
    r = _c.post("/packs/reload")
    check("重载成功", r.status_code, 200)
    check("返回健康度", "health" in r.json()["data"], True)


def test_fixture() -> None:
    print("\n[样例数据：前端可离线开发]")
    d = _c.get("/packs?fixture=1").json()["data"]
    check("标记为 fixture", d["fixture"], True)
    kinds = {r["kind"] for r in d["items"]}
    check("含未落地的 kind 样例", sorted(kinds), ["knowledge", "oracle"])
    row = next(r for r in d["items"] if r["provider"] == "learned")
    check("学习产出默认 draft", row["lifecycle"], "draft")
    check("学习产出默认不启用", row["enabled"], False)


def test_device(sn: str) -> None:
    print(f"\n[真机 dry-run sn={sn}]")
    r = _c.post(f"/packs/{RULE_UID}/dry-run?sn={sn}&package=com.mathmagic.magicam")
    if r.status_code == 409:
        print(f"  ⚠️ 设备未连通，跳过：{r.json().get('detail')}")
        return
    check("dry-run 成功", r.status_code, 200)
    d = r.json()["data"]
    check("默认不执行", d["executed"], False)
    check("给出计划动作", [a["capability"] for a in d["planned_actions"]],
          ["wake_screen", "wait_ms", "dismiss_keyguard", "wait_ms"])
    check("带设备事实", "awake" in d["evidence"], True)
    check("带层级信息", d["hierarchy"]["ok"], True)
    print(f"  ℹ️  matched={d['matched']} 证据={d['evidence_brief']} "
          f"层级={d['hierarchy']['nodes']} 节点/{d['hierarchy']['elapsed_ms']}ms "
          f"（{d['hierarchy']['source']}）")

    # advise 规则：应只产出建议文本，不给动作
    a = _c.post(f"/packs/builtin/recovery/system_permission_dialog/dry-run?sn={sn}").json()["data"]
    check("advise 规则不给动作", a["planned_actions"], [])
    check("advise 规则给建议文本", len(a["advice"]) > 20, True)


def main_() -> int:
    print("=== Packs 控制台 API 验收 ===")
    test_kinds()
    test_list_and_filters()
    test_detail()
    test_lifecycle_roundtrip()
    test_save_validation()
    test_dry_run_errors()
    test_reload()
    test_fixture()
    if len(sys.argv) > 1:
        test_device(sys.argv[1])
    else:
        print("\n（未指定 sn，跳过真机 dry-run；用法：verify_packs_api.py <sn>）")

    print("\n" + "=" * 46)
    if _fails:
        print(f"❌ {len(_fails)} 项失败：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
