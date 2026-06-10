# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
应用图谱页面识别 — 供 Copilot / 飞书回归 / 断言校验使用。

与 Agent Run 共用同一套骨架蒙版（AppNode.skeleton_config），
但不依赖 flow_id / Tool.vision，直接从 MobileEngine 截图识别。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from sqlalchemy.orm import Session, joinedload

from script.log import SLog
from server.core.vision.skeleton_algo import SkeletonAlgo
from server.models.AppGraph.app_structure import AppGraph, AppNode
from server.models.AppGraph.app_types import NodeType
from server.services.memory_context import _serialize_app_graph_structure

TAG = "PageContext"

DEFAULT_MIN_SCORE = 0.55
FIGMA_MIN_SCORE = 0.12
NAV_EXPECT_RE = re.compile(r"进入|跳转|打开|到达|导航|切换至|切到|列表成功|页面成功")
PAGE_TOKEN_RE = re.compile(
    r"(?:进入|打开|跳转至|导航至|到达|切换至|切到)\s*[「\"']?([^「」\"'\s，,；;]+)"
)


def load_app_graph_by_app_id(session: Session, app_id: str) -> Dict[str, Any]:
    """加载应用主图谱（最新一条）及序列化节点。"""
    if not app_id:
        return {"nodes": [], "edges": [], "graph_id": None}
    graph = (
        session.query(AppGraph)
        .filter(AppGraph.app_id == str(app_id))
        .order_by(AppGraph.created_at.desc())
        .first()
    )
    if not graph:
        return {"nodes": [], "edges": [], "graph_id": None}
    struct = _serialize_app_graph_structure(session, graph)
    struct["graph_id"] = graph.id
    struct["graph_name"] = graph.name or ""
    return struct


def capture_golden_frame(engine, *, count: int = 3) -> Optional[np.ndarray]:
    """从已 bootstrap 的 MobileEngine 采集去噪截图（BGR）。"""
    from script.sleep import mSleep

    if not engine or not hasattr(engine, "screenshot"):
        return None

    frames: List[np.ndarray] = []
    for _ in range(max(1, count)):
        try:
            shot = engine.screenshot()
        except Exception as e:
            SLog.w(TAG, f"screenshot failed: {e}")
            shot = None
        gray = _shot_to_bgr(shot)
        if gray is not None:
            frames.append(gray)
        if count > 1:
            mSleep(0.2)

    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]
    try:
        stack = np.stack(frames, axis=0)
        return np.median(stack, axis=0).astype(np.uint8)
    except Exception:
        return frames[0]


def _shot_to_bgr(shot) -> Optional[np.ndarray]:
    if shot is None:
        return None
    if isinstance(shot, str):
        img = cv2.imread(shot, cv2.IMREAD_COLOR)
        return img
    if hasattr(shot, "convert"):
        arr = np.array(shot.convert("RGB"))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    arr = np.asarray(shot)
    if arr.ndim == 2:
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    if arr.ndim == 3 and arr.shape[2] == 3:
        return arr
    return None


def build_skeleton_candidates(app_graph: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for node in app_graph.get("nodes") or []:
        if (node.get("type") or "").lower() not in ("page", str(NodeType.PAGE)):
            continue
        sk = node.get("skeleton_config") or {}
        master_path, mask_path, ignored_areas = SkeletonAlgo.skeleton_config_paths(sk)
        if not master_path or not mask_path:
            skipped.append(
                {
                    "node_id": node.get("id"),
                    "label": node.get("label"),
                    "reason": "no_skeleton",
                }
            )
            continue
        candidates.append(
            {
                "node_id": node.get("id"),
                "label": node.get("label"),
                "master_path": master_path,
                "mask_path": mask_path,
                "ignored_areas": ignored_areas,
                "screenshot": node.get("screenshot"),
                "_node": node,
            }
        )
    return candidates, skipped


def identify_current_page(
    engine,
    app_graph: Dict[str, Any],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    top_k: int = 5,
    frame_count: int = 3,
) -> Dict[str, Any]:
    """
    识别当前屏幕对应的应用图谱页面。

    返回::
        matched, node_id, label, score, method, rankings, candidates_count, skipped_pages
    """
    empty = {
        "matched": False,
        "node_id": None,
        "label": None,
        "score": 0.0,
        "method": "none",
        "rankings": [],
        "candidates_count": 0,
        "skipped_pages": [],
        "graph_id": app_graph.get("graph_id"),
    }
    candidates, skipped = build_skeleton_candidates(app_graph)
    empty["skipped_pages"] = skipped
    empty["candidates_count"] = len(candidates)
    if not candidates:
        return empty

    golden = capture_golden_frame(engine, count=frame_count)
    if golden is None:
        SLog.w(TAG, "identify: no screenshot from engine")
        return empty

    rankings = SkeletonAlgo.rank_page_candidates(golden, candidates, top_k=top_k)
    best = rankings[0] if rankings else None
    best_score = float(best.get("score") or 0.0) if best else 0.0
    matched = bool(best and best_score >= min_score)

    result = {
        "matched": matched,
        "node_id": best.get("node_id") if matched else None,
        "label": best.get("label") if matched else (best.get("label") if best else None),
        "score": best_score,
        "method": "skeleton",
        "rankings": [
            {
                "node_id": r.get("node_id"),
                "label": r.get("label"),
                "score": r.get("score"),
            }
            for r in rankings
        ],
        "candidates_count": len(candidates),
        "skipped_pages": skipped,
        "graph_id": app_graph.get("graph_id"),
    }
    if matched:
        SLog.i(
            TAG,
            f"📍 page={result['label']} score={best_score:.3f} "
            f"graph={app_graph.get('graph_id')}",
        )
    else:
        SLog.d(
            TAG,
            f"page identify below threshold best={result.get('label') or '-'} "
            f"score={best_score:.3f}",
        )
    return result


def enrich_page_context_screenshot(
    page_ctx: Dict[str, Any],
    *,
    sn: str,
    platform: str = "android",
    run_id: str = "",
    tag: str = "page",
) -> Dict[str, Any]:
    """为页面识别结果附加当前屏截图，供回放分析。"""
    out = dict(page_ctx or {})
    if out.get("screenshot") or not sn:
        return out
    try:
        from server.services.regression_capture import capture_device_screenshot

        url = capture_device_screenshot(
            sn, platform, run_id=run_id or "", tag=tag
        )
        if url:
            out["screenshot"] = url
    except Exception as e:
        SLog.w(TAG, f"page screenshot attach failed: {e}")
    return out


def identify_page_for_trace(
    app_id: str,
    engine,
    session: Optional[Session] = None,
    *,
    sn: str = "",
    platform: str = "android",
    run_id: str = "",
    tag: str = "page",
    screen_text: str = "",
    frame_count: int = 2,
) -> Dict[str, Any]:
    """识别当前页并附带截图。"""
    pc = identify_for_app(
        app_id,
        engine,
        session=session,
        frame_count=frame_count,
        screen_text=screen_text,
    )
    return enrich_page_context_screenshot(
        pc, sn=sn, platform=platform, run_id=run_id, tag=tag
    )


def identify_for_app(
    app_id: str,
    engine,
    session: Optional[Session] = None,
    *,
    min_score: float = FIGMA_MIN_SCORE,
    frame_count: int = 3,
    screen_text: str = "",
    use_skeleton: bool = False,
) -> Dict[str, Any]:
    """按 app_id 识别当前页：Figma 设计稿文案匹配优先，不依赖骨架训练图。"""
    if not app_id:
        return {"matched": False, "method": "none", "reason": "no_app_id"}

    own = session is None
    if own:
        from server.core.database import SessionLocal

        session = SessionLocal()
    try:
        blob = screen_text
        if not blob:
            blob = _collect_full_screen_text(engine)

        try:
            from server.services.page_navigation_service import (
                _screen_is_system_permission_dialog,
            )

            if _screen_is_system_permission_dialog(blob, engine=engine):
                return {
                    "matched": True,
                    "node_id": None,
                    "label": "系统权限弹窗",
                    "score": 0.95,
                    "method": "keyword",
                    "source": "keyword",
                    "graph_id": None,
                }
        except Exception:
            pass

        kw_hit = _identify_page_by_screen_keywords(blob)
        if kw_hit:
            kw_hit["graph_id"] = None
            return kw_hit

        from server.models.project import App
        from server.services.figma_logic_service import (
            identify_page_from_figma_logic,
            load_figma_logic_for_app,
        )

        app_graph = load_app_graph_by_app_id(session, app_id)
        app = session.query(App).filter(App.id == str(app_id)).first()
        logic = load_figma_logic_for_app(app) if app else None
        figma_hit: Dict[str, Any] = {"matched": False, "method": "none"}
        if logic:
            figma_hit = identify_page_from_figma_logic(
                blob, logic, min_score=min_score
            )
            rankings = figma_hit.get("rankings") or []
            best_label = figma_hit.get("label")
            best_score = float(figma_hit.get("score") or 0.0)
            if rankings and not best_label:
                best_label = rankings[0].get("label")
                best_score = float(rankings[0].get("score") or 0.0)
            if figma_hit.get("matched"):
                figma_hit["source"] = "figma"
                figma_hit["graph_id"] = app_graph.get("graph_id")
                figma_hit["label"] = best_label
                figma_hit["score"] = best_score
                return figma_hit
            if rankings:
                figma_hit["figma_rankings"] = rankings
                figma_hit["figma_best"] = best_label
                figma_hit["figma_score"] = best_score
                figma_hit["label"] = best_label
                figma_hit["score"] = best_score
                figma_hit["method"] = "figma_text"
                figma_hit["source"] = "figma"
                figma_hit["graph_id"] = app_graph.get("graph_id")
                figma_hit["matched"] = best_score >= min_score
                return figma_hit

        if use_skeleton and app_graph.get("graph_id") and app_graph.get("nodes"):
            skeleton = identify_current_page(
                engine,
                app_graph,
                min_score=DEFAULT_MIN_SCORE,
                frame_count=frame_count,
            )
            if skeleton.get("matched"):
                skeleton["source"] = "skeleton"
                return skeleton
            if figma_hit.get("figma_best"):
                skeleton["figma_rankings"] = figma_hit.get("figma_rankings")
                skeleton["figma_best"] = figma_hit.get("figma_best")
                skeleton["figma_score"] = figma_hit.get("figma_score")
                skeleton["label"] = figma_hit.get("figma_best")
                skeleton["score"] = figma_hit.get("figma_score")
            skeleton["source"] = skeleton.get("method") or "none"
            return skeleton

        empty: Dict[str, Any] = {
            "matched": False,
            "node_id": None,
            "label": figma_hit.get("figma_best") or figma_hit.get("label"),
            "score": figma_hit.get("figma_score") or figma_hit.get("score") or 0.0,
            "method": figma_hit.get("method") or "none",
            "rankings": [],
            "graph_id": app_graph.get("graph_id"),
            "source": "figma" if logic else "none",
        }
        if figma_hit.get("figma_rankings"):
            empty["figma_rankings"] = figma_hit["figma_rankings"]
            empty["figma_best"] = figma_hit.get("figma_best")
            empty["figma_score"] = figma_hit.get("figma_score")
        return empty
    finally:
        if own and session is not None:
            session.close()


def _screen_cache_watermark() -> int:
    try:
        from server.services.regression_run_context import get_ctx

        ctx = get_ctx()
        if ctx is not None:
            return len(ctx.get("gestures") or [])
    except Exception:
        pass
    return -1


def invalidate_engine_screen_cache(engine=None) -> None:
    """手势或强制刷新后清除引擎上的屏快照。"""
    if engine is not None:
        try:
            delattr(engine, "_mo_screen_snap")
        except Exception:
            pass
    try:
        from driver.agent.Crawl.device_bootstrap import _ENGINE_CACHE

        for entry in _ENGINE_CACHE.values():
            eng = entry.get("engine")
            if eng is not None:
                try:
                    delattr(eng, "_mo_screen_snap")
                except Exception:
                    pass
    except Exception:
        pass


def get_engine_screen_snapshot(engine, *, force: bool = False) -> Dict[str, Any]:
    """
    单次截图 + 单次 OCR + 层级文本，同一手势水位内复用。
    返回 {ocr_text, blob, shot, wm}。
    """
    wm = _screen_cache_watermark()
    if not force and engine is not None:
        cached = getattr(engine, "_mo_screen_snap", None)
        if isinstance(cached, dict) and cached.get("wm") == wm and cached.get("blob"):
            return cached

    w, h = 1080, 1920
    try:
        if hasattr(engine, "screen_size"):
            w, h = engine.screen_size()
        elif hasattr(engine, "_display_size"):
            w, h = engine._display_size()
    except Exception:
        pass

    hierarchy_lines: List[str] = []
    try:
        from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy

        for t in discover_clickables_from_hierarchy(engine, w, h, max_items=80):
            if t.label:
                hierarchy_lines.append(t.label)
    except Exception as e:
        SLog.w(TAG, f"hierarchy collect failed: {e}")

    ocr_lines: List[str] = []
    shot = None
    try:
        if hasattr(engine, "screenshot"):
            shot = engine.screenshot()
            if shot is not None:
                from driver.agent.Crawl.ui_discovery import _ocr_analyze_shot

                for it in _ocr_analyze_shot(shot) or []:
                    t = (it.get("text") or "").strip()
                    if t:
                        ocr_lines.append(t)
    except Exception as e:
        SLog.w(TAG, f"screen ocr failed: {e}")

    ocr_text = "\n".join(ocr_lines)
    parts = []
    if hierarchy_lines:
        parts.append("\n".join(hierarchy_lines))
    if ocr_lines:
        parts.append(ocr_text)
    blob = "\n".join(parts)

    snap: Dict[str, Any] = {
        "wm": wm,
        "ocr_text": ocr_text,
        "blob": blob,
        "screen_w": w,
        "screen_h": h,
    }
    if engine is not None:
        engine._mo_screen_snap = snap
    try:
        from server.services.regression_run_context import get_ctx

        ctx = get_ctx()
        if ctx is not None:
            ctx["screen_blob"] = blob
            ctx["screen_ocr"] = ocr_text
            ctx["screen_wm"] = wm
    except Exception:
        pass
    return snap


def _collect_full_screen_text(engine, *, force: bool = False) -> str:
    """层级 + 全屏 OCR 合并（单次截图，避免重复 OCR）。"""
    return get_engine_screen_snapshot(engine, force=force).get("blob") or ""


def collect_ocr_text(engine, *, force: bool = False) -> str:
    """仅可见 OCR 文本（与 page_navigation 共用快照）。"""
    return get_engine_screen_snapshot(engine, force=force).get("ocr_text") or ""


def _collect_screen_text_from_engine(engine) -> str:
    return _collect_full_screen_text(engine)


def _identify_page_by_screen_keywords(screen_text: str) -> Optional[Dict[str, Any]]:
    """基于 OCR/层级关键词的快速页面识别（无需骨架截图）。"""
    blob = (screen_text or "").strip()
    if not blob:
        return None

    try:
        from server.services.page_navigation_service import (
            _screen_is_consent_dialog,
            _screen_is_login_home,
            _screen_is_login_surface,
            _screen_is_phone_login_form,
            _screen_is_user_agreement_page,
        )
    except Exception:
        _screen_is_consent_dialog = None  # type: ignore
        _screen_is_login_home = None  # type: ignore
        _screen_is_user_agreement_page = None  # type: ignore

    login_markers = (
        "一键登录",
        "本机号码",
        "本机号码一键登录",
        "手机号登录",
        "验证码登录",
        "其他登录",
        "其他方式登录",
        "其他登录方式",
        "请输入手机号",
        "发送验证码",
        "访客浏览",
        "登录中",
        "正在登录",
    )
    login_hits = sum(1 for k in login_markers if k in blob)
    is_login = (
        login_hits >= 1
        or (callable(_screen_is_login_surface) and _screen_is_login_surface(blob))
        or (callable(_screen_is_phone_login_form) and _screen_is_phone_login_form(blob))
        or (callable(_screen_is_login_home) and _screen_is_login_home(blob))
    )

    if is_login:
        return {
            "matched": True,
            "node_id": None,
            "label": "登录注册页",
            "score": 0.88 + min(0.1, login_hits * 0.02),
            "method": "keyword",
            "source": "keyword",
        }

    if callable(_screen_is_consent_dialog) and _screen_is_consent_dialog(blob):
        return {
            "matched": True,
            "node_id": None,
            "label": "隐私 consent 弹窗",
            "score": 0.92,
            "method": "keyword",
            "source": "keyword",
        }

    if callable(_screen_is_user_agreement_page) and _screen_is_user_agreement_page(blob):
        return {
            "matched": True,
            "node_id": None,
            "label": "用户协议页",
            "score": 0.88,
            "method": "keyword",
            "source": "keyword",
        }

    consent = "不同意" in blob and any(w in blob for w in ("同意", "同意并继续", "接受"))
    if consent:
        return {
            "matched": True,
            "node_id": None,
            "label": "隐私 consent 弹窗",
            "score": 0.92,
            "method": "keyword",
            "source": "keyword",
        }

    if (
        sum(1 for k in ("用户协议", "平台用户协议", "造好物 - 平台") if k in blob) >= 1
        and "不同意" not in blob
        and "不同意" not in blob
        and len(blob) > 200
    ):
        return {
            "matched": True,
            "node_id": None,
            "label": "用户协议页",
            "score": 0.88,
            "method": "keyword",
            "source": "keyword",
        }

    if any(k in blob for k in ("登录中", "正在登录", "一键登录", "访客浏览")):
        return None

    home_tabs = ("首页", "消息", "我的", "想要", "造物秀", "AI创意", "想要成真")
    home_hits = sum(1 for k in home_tabs if k in blob)
    if home_hits >= 2 or (
        home_hits >= 1 and any(k in blob for k in ("推荐", "关注", "发现", "Feed", "feed"))
    ):
        return {
            "matched": True,
            "node_id": None,
            "label": "首页",
            "score": 0.8 + min(0.15, home_hits * 0.05),
            "method": "keyword",
            "source": "keyword",
        }
    return None


def verify_page_node(engine, node_data: Dict[str, Any]) -> Dict[str, Any]:
    """用 Feedback 对指定图谱节点做视觉核验（骨架 + OCR 锚点）。"""
    try:
        from driver.agent.Perception.Vision.feedback import Feedback

        fb = Feedback()
        ok = bool(fb.verify_current_page(node_data))
        return {"ok": ok, "method": "feedback_verify", "label": node_data.get("label")}
    except Exception as e:
        SLog.w(TAG, f"verify_page_node failed: {e}")
        return {"ok": False, "method": "feedback_verify", "msg": str(e)}


def extract_page_tokens(expected: str) -> List[str]:
    """从预期文案提取目标页面关键词。"""
    from server.services.expectation_semantic_service import normalize_page_intent

    exp = (expected or "").strip()
    if not exp:
        return []
    tokens: List[str] = []

    def _add(c: str) -> None:
        c = (c or "").strip()
        if len(c) < 1:
            return
        if c not in tokens:
            tokens.append(c)
        norm = normalize_page_intent(c)
        if norm and norm not in tokens:
            tokens.append(norm)

    m = PAGE_TOKEN_RE.search(exp)
    if m:
        _add(m.group(1))
    for suffix in ("列表", "页面", "首页", "界面", "成功", "页"):
        if exp.endswith(suffix) and len(exp) > len(suffix):
            _add(exp[: -len(suffix)])
    parts = re.split(r"[、,，;；/\\s]+", exp)
    for p in parts:
        p = re.sub(r"^(进入|打开|跳转|导航|到达|切换)", "", p).strip()
        if len(p) >= 1:
            _add(p)
    _add(normalize_page_intent(exp))
    return tokens


def is_navigation_expectation(expected: str) -> bool:
    return bool(NAV_EXPECT_RE.search(expected or ""))


def expected_matches_page(expected: str, page_ctx: Dict[str, Any]) -> Optional[bool]:
    """
    用页面识别结果校验导航类预期。
    返回 True/False 表示可判定；None 表示图谱无法判定，应回退文案匹配。
    """
    from server.services.expectation_semantic_service import pages_semantically_match

    if not page_ctx.get("matched"):
        return None
    label = (page_ctx.get("label") or page_ctx.get("figma_best") or "").strip()
    if not label:
        return None

    if pages_semantically_match(expected, label):
        return True

    tokens = extract_page_tokens(expected)
    if not tokens:
        if is_navigation_expectation(expected):
            return None
        return None
    label_l = label.lower()
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        if pages_semantically_match(t, label):
            return True
        if t in label or t.lower() in label_l:
            return True
        if label in t or label.lower() in t.lower():
            return True
    if is_navigation_expectation(expected):
        return False
    return None


def find_graph_node_by_label(app_graph: Dict[str, Any], label_hint: str) -> Optional[Dict[str, Any]]:
    hint = (label_hint or "").strip().lower()
    if not hint:
        return None
    best = None
    best_len = -1
    for node in app_graph.get("nodes") or []:
        nl = (node.get("label") or "").strip().lower()
        if not nl:
            continue
        if hint in nl or nl in hint:
            if len(nl) > best_len:
                best = node
                best_len = len(nl)
    return best


def _fmt_score(score: Any) -> str:
    try:
        if score is None:
            return ""
        return f"{float(score):.0%}"
    except (TypeError, ValueError):
        return ""


def enrich_check_with_page(
    expected: str,
    page_ctx: Dict[str, Any],
    *,
    steps_ok: bool,
    screen_text: str = "",
) -> Optional[Dict[str, Any]]:
    """若页面识别可判定预期，返回 {ok, reason}；否则 None。"""
    if not steps_ok:
        return None
    try:
        from server.services.expectation_semantic_service import _is_home_intent, normalize_page_intent

        exp_norm = normalize_page_intent(expected)
        if _is_home_intent(exp_norm, expected):
            blob = screen_text or ""
            login_markers = (
                "一键登录",
                "本机号码",
                "访客浏览",
                "验证码登录",
                "密码登录",
                "登录中",
                "正在登录",
            )
            if sum(1 for k in login_markers if k in blob) >= 1:
                return {"ok": False, "reason": "界面仍为登录页，未进入首页"}
        if "造物秀" in expected and "造物秀" in (screen_text or ""):
            cur = (page_ctx or {}).get("label") or ""
            if cur == "首页" or _is_home_intent(normalize_page_intent(cur), cur):
                return {"ok": True, "reason": "顶栏已切换至「造物秀」"}
    except Exception:
        pass
    outcome = evaluate_outcome_expectation(
        expected,
        page_ctx=page_ctx,
        screen_text=screen_text,
        steps_ok=steps_ok,
    )
    if outcome is not None:
        return outcome
    if not page_ctx:
        return None
    verdict = expected_matches_page(expected, page_ctx)
    if verdict is True:
        score = page_ctx.get("score") or 0
        src = "Figma 设计稿" if page_ctx.get("method") == "figma_text" else "应用图谱"
        pct = _fmt_score(score)
        return {
            "ok": True,
            "reason": f"页面识别({src})匹配「{page_ctx.get('label')}」({pct})" if pct else f"页面识别({src})匹配「{page_ctx.get('label')}」",
        }
    if verdict is False:
        try:
            from server.services.expectation_semantic_service import judge_navigation_expectation

            semantic = judge_navigation_expectation(
                expected,
                page_ctx,
                screen_text=screen_text,
                use_llm=True,
            )
            if semantic is not None:
                return semantic
        except Exception as e:
            SLog.w(TAG, f"semantic expectation judge failed: {e}")

        cur = page_ctx.get("label") or "未知页"
        score = page_ctx.get("score")
        src = "Figma 设计稿" if page_ctx.get("method") == "figma_text" else "应用图谱"
        pct = _fmt_score(score)
        tail = f"({pct})" if pct else ""
        return {
            "ok": False,
            "reason": f"页面识别({src})：当前「{cur}」{tail}，与预期「{expected}」不符",
        }
    return None


def format_page_hint(page_ctx: Dict[str, Any]) -> str:
    if not page_ctx:
        return ""
    label = (
        page_ctx.get("label")
        or page_ctx.get("figma_best")
        or page_ctx.get("current_page_label")
    )
    score = page_ctx.get("score") or page_ctx.get("figma_score")
    if not label:
        return ""
    src = "Figma" if page_ctx.get("method") == "figma_text" or page_ctx.get("figma_best") else "图谱"
    pct = _fmt_score(score)
    if page_ctx.get("matched"):
        return f"当前页「{label}」（{src} 置信 {pct}）" if pct else f"当前页「{label}」（{src}）"
    if pct:
        return f"当前页可能为「{label}」（{src} {pct}，未达阈值）"
    return f"当前页可能为「{label}」（{src}）"


def evaluate_outcome_expectation(
    expected: str,
    *,
    page_ctx: Optional[Dict[str, Any]] = None,
    screen_text: str = "",
    steps_ok: bool = False,
) -> Optional[Dict[str, Any]]:
    """登录/手机号等业务结果类预期（非导航类）的语义判定。"""
    exp = (expected or "").strip()
    if not exp:
        return None
    blob = screen_text or ""
    login_markers = ("一键登录", "本机号码", "手机号登录", "验证码登录", "其他登录")
    on_login = sum(1 for k in login_markers if k in blob) >= 1

    if re.search(r"登录成功|成功登录|完成登录", exp):
        if on_login:
            return {"ok": False, "reason": "仍在登录页，登录未完成"}
        if steps_ok:
            cur = (page_ctx or {}).get("label") or (page_ctx or {}).get("figma_best") or ""
            if cur and "登录" not in cur:
                return {
                    "ok": True,
                    "reason": f"已离开登录页，当前在「{cur}」",
                }
            return {"ok": True, "reason": "已离开登录页，登录流程完成"}
        return None

    if re.search(r"获取手机号|手机号获取|本机号码", exp):
        if re.search(r"1\d{2}[*＊]{2,}\d{3,4}|\+86\s*1\d{2}", blob):
            return {"ok": True, "reason": "界面可见脱敏手机号"}
        if steps_ok and not on_login:
            return {"ok": True, "reason": "登录流程已执行，本机号码已用于登录"}
        if on_login and re.search(r"1\d{2}[*＊]", blob):
            return {"ok": True, "reason": "登录页已展示本机号码"}
        return None

    return None
