"""检查脑图树转成画板 mind_map 节点的结果：一个根节点、父子用批次内 id 串起来、空壳节点被跳过。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.feishu_wiki_service import (
    _retire_history,
    _wiki_history,
    mindmap_to_board_nodes,
)

TREE = {
    "kind": "root",
    "text": "会员支付改版",
    "children": [
        {
            "kind": "platform",
            "text": "iOS",
            "children": [
                {
                    "kind": "module",
                    "text": "支付",
                    "children": [
                        {"kind": "point", "text": "微信支付成功", "detail": "订单状态变已付款"},
                        {"kind": "point", "text": "支付取消"},
                    ],
                }
            ],
        },
        {"kind": "module", "text": "", "children": [{"kind": "point", "text": "壳节点下的测试点"}]},
        {"kind": "point", "text": "", "children": []},
    ],
}

# 两个端 + 每端多个模块：配色该落在模块那一层
TREE_TWO_ENDS = {
    "kind": "root",
    "text": "会员支付改版",
    "children": [
        {
            "text": end,
            "children": [
                {"text": mod, "children": [{"text": f"{mod} 测试点"}]}
                for mod in ("支付", "订单", "退款")
            ],
        }
        for end in ("iOS", "Android")
    ],
}

# 没有端这一层：配色该落在一级
TREE_FLAT = {
    "kind": "root",
    "text": "会员支付改版",
    "children": [{"text": mod, "children": [{"text": f"{mod} 测试点"}]} for mod in ("支付", "订单", "退款", "对账")],
}


def fills(nodes: list[dict], parent: str) -> list[str]:
    return [
        n["style"].get("fill_color")
        for n in nodes
        if (n.get("mind_map_node") or {}).get("parent_id") == parent
    ]


def check_hue() -> list[str]:
    """配色应该落在分支多的那一层：一级只有两个「端」时下移到模块。"""
    out: list[str] = []

    two = mindmap_to_board_nodes(TREE_TWO_ENDS, "两个端")
    ends = fills(two, "m0")
    if len(set(ends)) != 1:
        out.append(f"只有两个端时，端本身该用统一的结构色，实际 {ends}")
    ios = next(n for n in two if n["text"]["text"] == "iOS")
    modules = fills(two, ios["id"])
    if len(set(modules)) != len(modules):
        out.append(f"同一个端下的模块该各用一色，实际 {modules}")
    android = next(n for n in two if n["text"]["text"] == "Android")
    if set(modules) & set(fills(two, android["id"])):
        out.append("不同端下的模块也该错开颜色，不该撞色")

    many = mindmap_to_board_nodes(TREE_FLAT, "没有端这一层")
    tops = fills(many, "m0")
    if len(set(tops)) != len(tops):
        out.append(f"没有端这一层时，一级模块该各用一色，实际 {tops}")
    return out


def check_history() -> list[str]:
    """旧文档改名后，历史里指向它的记录该跟着改名并标成已归档，别的不动。"""
    out: list[str] = []
    req = {
        "mindmap_wiki_history": [
            {"node_token": "n2", "title": "脑图", "nodes": 40},
            {"node_token": "n1", "title": "脑图", "nodes": 30},
            "坏数据",
        ]
    }
    history = _wiki_history(req)
    if len(history) != 2:
        out.append(f"非 dict 的脏数据该被过滤掉，实际 {len(history)} 条")
    if history is req["mindmap_wiki_history"]:
        out.append("该返回拷贝，不然会就地改到入参")

    _retire_history(history, "n1", "脑图（旧版 08-24 17:26）")
    old = next(x for x in history if x["node_token"] == "n1")
    keep = next(x for x in history if x["node_token"] == "n2")
    if old.get("title") != "脑图（旧版 08-24 17:26）" or not old.get("retired"):
        out.append(f"被让开的那条该改名并标归档，实际 {old}")
    if keep.get("retired") or keep.get("title") != "脑图":
        out.append(f"其它记录不该被动，实际 {keep}")
    if req["mindmap_wiki_history"][1].get("retired"):
        out.append("入参里的原始记录被改坏了")
    return out


def check_hide() -> list[str]:
    """设为失效：移入失效列表，上一份变当前并恢复脑图。"""
    from unittest.mock import patch
    from server.services.feishu_wiki_service import invalidate_mindmap_wiki, _latest_mindmap_dialogue

    out: list[str] = []
    prev_tree = {
        "title": "旧版",
        "children": [{"id": "a", "text": "App", "kind": "platform", "children": [
            {"id": "f1", "text": "功能A", "kind": "feature", "children": [
                {"id": "p1", "text": "点1", "kind": "point", "children": []},
            ]},
        ]}],
    }
    doc = {
        "requirements": [
            {
                "id": "r1",
                "title": "新人礼",
                "analyst_feedback": "漏了后台",
                "cover_history": [
                    {"job": "draft_mindmap", "note": "入口在我的", "at": "2026-08-25T16:40:00"},
                ],
                "mindmap": {"title": "新版膨胀", "children": [{"text": "很多点"}]},
                "mindmap_wiki": {
                    "node_token": "n3",
                    "title": "新人礼 测试脑图",
                    "url": "https://x/n3",
                    "space_id": "sp",
                    "nodes": 213,
                },
                "mindmap_wiki_history": [
                    {
                        "node_token": "n3",
                        "title": "新人礼 测试脑图",
                        "url": "https://x/n3",
                        "nodes": 213,
                        "at": "2026-08-25T16:48:00",
                        "retired": False,
                        "invalid": False,
                        "dialogue": "入口在我的",
                    },
                    {
                        "node_token": "n2",
                        "title": "新人礼 测试脑图（旧版 08-25 16:20）",
                        "url": "https://x/n2",
                        "nodes": 112,
                        "at": "2026-08-25T16:20:00",
                        "retired": True,
                        "invalid": False,
                        "mindmap_snapshot": prev_tree,
                    },
                ],
            }
        ]
    }
    if _latest_mindmap_dialogue(doc["requirements"][0]) != "入口在我的":
        out.append("latest dialogue 应从 cover_history 取")

    with patch("server.services.feishu_wiki_service._retire_node", return_value="新人礼 测试脑图（旧版 08-25 17:00）"):
        result = invalidate_mindmap_wiki(doc, requirement_id="r1", node_token="n3")
    req = next(r for r in result["qa_process"]["requirements"] if r["id"] == "r1")
    if req["mindmap_wiki"].get("node_token") != "n2":
        out.append(f"失效后当前该是 n2，实际 {req['mindmap_wiki'].get('node_token')}")
    hist = req["mindmap_wiki_history"]
    n3 = next(x for x in hist if x["node_token"] == "n3")
    n2 = next(x for x in hist if x["node_token"] == "n2")
    if not n3.get("invalid") or not n3.get("retired"):
        out.append("被失效的该标 invalid+retired")
    if n2.get("invalid") or n2.get("retired"):
        out.append("上一份不该再是 invalid/retired")
    if "旧版" in str(n2.get("title") or ""):
        out.append(f"上一份标题该去掉旧版后缀，实际 {n2.get('title')}")
    if not result.get("mindmap_restored"):
        out.append("应恢复上一份脑图快照")
    if str((req.get("mindmap") or {}).get("title") or "") != "旧版":
        out.append(f"应用内脑图该切回旧版，实际 {req.get('mindmap')}")
    return out


def check_hierarchy() -> list[str]:
    """测试点不得与功能同级挂在模块下。"""
    from server.services.qa_role_jobs import _normalize_mindmap_hierarchy

    out: list[str] = []
    messy = {
        "title": "t",
        "children": [{
            "text": "App",
            "kind": "platform",
            "children": [{
                "text": "首页",
                "kind": "module",
                "children": [{
                    "text": "新用户banner",
                    "kind": "module",
                    "children": [
                        {"text": "新人特惠优惠券展示", "kind": "feature", "children": [
                            {"text": "新用户可见banner", "kind": "point", "children": []},
                        ]},
                        {"text": "新用户进入App首页新用户banner位展示新人特惠优惠券信息", "kind": "point", "children": []},
                        {"text": "老用户进入首页无新人特惠优惠券banner", "kind": "point", "children": []},
                    ],
                }],
            }],
        }],
    }
    fixed = _normalize_mindmap_hierarchy(messy)
    banner = fixed["children"][0]["children"][0]["children"][0]
    kids = banner.get("children") or []
    kinds = [c.get("kind") for c in kids]
    if "point" in kinds:
        out.append(f"模块下仍有测试点同级：{kinds}")
    feat = next((c for c in kids if c.get("text") == "新人特惠优惠券展示"), None)
    if not feat:
        out.append("功能节点丢了")
    else:
        pts = [c.get("text") for c in (feat.get("children") or [])]
        if "新用户可见banner" not in pts:
            out.append("原功能下的点丢了")
        if not any("新用户进入App首页" in (t or "") for t in pts):
            out.append("同级误挂的点没归到功能下")
    return out


def main() -> int:
    nodes = mindmap_to_board_nodes(TREE, "会员支付改版 测试脑图")
    by_id = {n["id"]: n for n in nodes}
    fails: list[str] = []

    roots = [n for n in nodes if "mind_map_root" in n]
    if len(roots) != 1:
        fails.append(f"根节点应该只有 1 个，实际 {len(roots)}")
    elif roots[0]["text"]["text"] != "会员支付改版 测试脑图":
        fails.append("根节点文字应该是传进来的标题")

    for node in nodes:
        if node["type"] != "mind_map":
            fails.append(f"{node['id']} 的 type 应该是 mind_map")
        if not node.get("width") or not node.get("height"):
            fails.append(f"{node['id']} 缺 width/height，画板会拒")
        if "mind_map_root" in node:
            continue
        pid = (node.get("mind_map_node") or {}).get("parent_id")
        if pid not in by_id:
            fails.append(f"{node['id']} 的 parent_id={pid!r} 不在同一批节点里")

    texts = [n["text"]["text"] for n in nodes]
    if "微信支付成功 · 订单状态变已付款" not in texts:
        fails.append("测试点应该带上 detail")
    if "壳节点下的测试点" not in texts:
        fails.append("没文字的壳节点应该被跳过，孩子往上提")
    if len(nodes) != 6:
        fails.append(f"节点数应为 6（1 根 + 5 个），实际 {len(nodes)}")

    shell = next((n for n in nodes if n["text"]["text"] == "壳节点下的测试点"), None)
    if shell and (shell.get("mind_map_node") or {}).get("parent_id") != "m0":
        fails.append("壳节点的孩子应该直接挂到根节点上")

    if any("_shape" in n for n in nodes):
        fails.append("_shape 是内部字段，不该发给飞书")
    shapes = {n["text"]["text"]: (n.get("mind_map_root") or n.get("mind_map_node") or {}).get("type") for n in nodes}
    if shapes.get("会员支付改版 测试脑图") != "mind_map_full_round_rect":
        fails.append("根节点应该是实心圆角矩形")
    if shapes.get("iOS") != "mind_map_full_round_rect":
        fails.append("一级分支应该是实心圆角矩形")
    if shapes.get("支付") != "mind_map_round_rect":
        fails.append("二级节点应该是描边圆角矩形")
    if shapes.get("支付取消") != "mind_map_text":
        fails.append("末端测试点应该是纯文字节点，不画框")

    leaf = next(n for n in nodes if n["text"]["text"] == "支付取消")
    if leaf["style"].get("border_style") != "none":
        fails.append("纯文字节点不该有边框")
    root = next(n for n in nodes if "mind_map_root" in n)
    if root["text"]["font_size"] <= leaf["text"]["font_size"]:
        fails.append("根节点字号应该比末端节点大")
    if root["mind_map_root"].get("layout") != "left_right":
        fails.append("默认该是左右布局的思维导图，不是树状图")
    sides = [
        (n.get("mind_map_node") or {}).get("layout_position")
        for n in nodes
        if (n.get("mind_map_node") or {}).get("parent_id") == "m0"
    ]
    if sides != ["right", "left"]:
        fails.append(f"一级分支该左右交替，实际 {sides}")
    if any((n.get("mind_map_node") or {}).get("layout_position") for n in nodes if (n.get("mind_map_node") or {}).get("parent_id") != "m0"):
        fails.append("layout_position 只对根节点的直接子节点生效，别往下发")

    fails.extend(check_hue())
    fails.extend(check_history())
    fails.extend(check_hide())
    fails.extend(check_hierarchy())

    print(json.dumps(nodes, ensure_ascii=False, indent=2))
    for msg in fails:
        print(f"FAIL {msg}")
    print("OK" if not fails else "FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
