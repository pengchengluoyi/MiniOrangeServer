# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AgentExecutor：目标导向的闭环执行引擎（D1–D6 改造，仅 adb 通道）。

替代旧的「整体 plan → 逐条跑 → 文本盲 replan」两段式。核心循环：

    observe(每步截图) → decide_next_action(看图直接出坐标) → router.dispatch → re-observe

- D1 用例=目标+检查点   D2 决策 VLM 直接出坐标(不走 locate VLM)   D3 每步看图
- D4 成功交给 VLM 断言   D5 允许 ask_human(走 human_* 能力+现有 HitlExecutor)
- 收尾：步数预算 + 成功断言 + 震荡检测（取代 max_replans 硬上限）

底座全复用：CapabilityRouter / executors / 通道 / screen / case_memory。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression import planner
from server.services.ai.regression.schemas import (
    AgentAction,
    CaseGoal,
    CaseSpec,
    EventResult,
    EventStatus,
    PlanEvent,
    RunReport,
)
from server.services.regression.router import CapabilityRouter
from server.services.regression.screen import capture_screen
from server.services.regression import agent_stream
from server.services.runtime.run_context import RunContext

TAG = "AgentExecutor"

# 不计入决策预算的能力（加载轮询）
_WAIT_CAPS = {"wait_ms", "wait_screen_ready"}
# 等待 / 断言 / 开场跳过 不是「动作没落地」。cr-00c044bb6529 五条用例都在
# 连续 wait_ms（轮播引导语、加载进度条）上被误判卡死。这些能力另有
# max_wait_rounds 等上限，不进震荡窗口。
_OSCILLATION_IGNORE_CAPS = {
    "wait_ms", "wait_screen_ready", "assert_visual",
    "skip_restart", "inspect_session", "pick_account", "exec_script", "capture_screen",
    "skip_repeat_tap",
}
_MUTATE_CAPS = {
    "tap_element", "swipe_element_to_element", "swipe_direction",
    "input_text", "press_key",
}
# 同一入口再点：不检测页面有没有切过去，直接做后续操作。
_PROCEED_AFTER_ENTRY = (
    "【入口已点过】同一位置已经点过。不要检测页面有没有切换，也不要再点这里。"
    "立刻做目标的后续操作：未完成的检查点或成功标准（用 assert_visual 看目标是否出现，或继续其它步骤）。"
)
# 导航是否成功不要靠「选中态/切没切页」挡后续；这些检查点直接跳过。
_NAV_VERIFY_RE = re.compile(
    r"选中态|已选中|底部.{0,16}(首页|tab|导航)|tab.{0,8}选中",
    re.I,
)
_NESTED_PUBLISH_RE = re.compile(
    r"再发|发一条新|发布一条新|新帖后|发布新帖|再发布",
)
_PUBLISHED_RE = re.compile(r"发布成功|已发布到|发布完成")
_PROCESS_HINT_RE = re.compile(r"加载占位|加载中|生成中|切换中|转圈|占位|白屏")
_LOGOUT_RE = re.compile(r"退出登录|登出|切换账号")
_EMPTY_FEED_RE = re.compile(r"无内容|空态|空社区|少内容|很少内容|游客|未登录环境")
_CLEAR_ENV_RE = re.compile(r"清除(应用)?数据|清缓存|重置账号")
_DELETE_POSTS_RE = re.compile(r"删除.{0,8}(发布|帖|内容|作品)")
_PERSONAL_EMPTY_RE = re.compile(r"我的发布|个人(页|中心)?|作品集|已发布作品")
_DEVICE_OP_RE = re.compile(
    r"勾选|请你.{0,16}(登录|操作)|完成登录后|在(设备|手机|真机)上|"
    r"去(设备|手机).{0,10}(点|登|操作)|输入[「\"']?已登录|点同意|微信登录"
)
_HITL_FIELD_Q = {
    "phone": "请输入登录用的11位手机号，系统会填进登录页",
    "sms_code": "请输入短信验证码（4-8位数字），系统会填进验证码框",
    "text": "请输入需要填进界面的文本",
}

# 开场/旁路步骤不进 decide 历史：模型看的是当前截图，这些只会挤掉真正做过的动作
_HISTORY_NOISE_CAPS = {
    "skip_restart", "inspect_session", "pick_account",
    "capture_screen", "noop",
}
_HISTORY_BLOCK_MAX_CHARS = 900


def _clip_hist(text: str, n: int) -> str:
    s = str(text or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def history_action_brief(cap: str, params: dict | None) -> str:
    """历史行里的动作摘要：保留「点了哪 / 填了什么」，丢掉整份 params。"""
    cap = str(cap or "").strip()
    params = params if isinstance(params, dict) else {}
    if cap in ("tap_element", "click"):
        x, y = params.get("x"), params.get("y")
        if x is not None and y is not None:
            return f"{cap} @{x},{y}"
        return cap or "?"
    if cap == "swipe_element_to_element":
        return (
            f"{cap} @{params.get('from_x')},{params.get('from_y')}"
            f"→{params.get('to_x')},{params.get('to_y')}"
        )
    if cap == "swipe_direction":
        return f"{cap} {params.get('direction') or ''}".strip()
    if cap == "input_text":
        text = _clip_hist(params.get("text"), 20)
        return f"{cap} 「{text}」" if text else cap
    if cap == "press_key":
        return f"{cap} {params.get('key') or ''}".strip()
    if cap in ("wait_ms", "wait_screen_ready"):
        ms = params.get("ms") or params.get("timeout_ms")
        return f"{cap} {ms}ms" if ms else cap
    if cap == "assert_visual":
        return f"{cap} {_clip_hist(params.get('expectation'), 28)}".strip()
    skip = {
        "x", "y", "from_x", "from_y", "to_x", "to_y",
        "memory_context", "package", "image_base64",
    }
    bits: list[str] = []
    for key, val in params.items():
        if key in skip or val in (None, "", [], {}):
            continue
        bits.append(f"{key}={_clip_hist(val, 18)}")
        if len(bits) >= 2:
            break
    return f"{cap} {' '.join(bits)}".strip() if bits else (cap or "?")

# ask_human 用的 human_* 能力
_HUMAN_CAPS = {
    "human_confirm", "human_choice_single", "human_choice_multiple",
    "human_input_text", "human_upload_image", "human_acknowledge",
}

# 统一失败分类标签（供 UI 展示，让同一真因永远归到同一类）
_CATEGORY_LABEL = {
    "success": "成功",
    "goal_unreachable": "目标不可达/环境不符",
    "execution_error": "执行异常(点击/截图/设备)",
    "budget_exhausted": "步数耗尽",
    "needs_human": "需人工介入",
    "device_unhealthy": "设备/系统异常",
}


# 路径/入口类知识：标题、分类、标签命中即视为「怎么走」说明（与具体业务词无关）
_PATH_KNOWLEDGE_RE = re.compile(
    r"如何进入|怎么进|如何打开|怎么打开|入口|路径|操作方式|操作说明|导航|定位方式|步骤说明|怎么去",
    re.I,
)
# 决策看起来在选入口/跳转时，才做「是否遵循路径知识」的纠正
_NAV_ACT_RE = re.compile(r"进入|点击|打开|前往|切换到|跳到|底部|顶部|导航|tab", re.I)
_KNOWLEDGE_TOKEN_STOP = {
    "点击", "输入", "勾选", "页面", "步骤", "进行", "成功", "失败", "登录", "打开",
    "关闭", "测试", "用例", "操作", "验证", "检查", "进入", "以后", "之后", "然后",
    "可以", "需要", "当前", "屏幕", "应用", "如何", "怎么", "以及", "或者", "一个",
    "任意", "说明", "方式", "正确", "本应用", "请按", "实际", "界面", "补充",
}


@dataclass
class AgentOptions:
    max_steps: int = 25                  # 决策预算（wait 不占）
    steps_per_case_step: int = 5         # 飞书 1 步 → 最多 N 次决策
    min_steps: int = 15
    max_steps_cap: int = 60
    max_wait_rounds: int = 15            # 连续 wait 上限，防止无限等
    max_create_steps: int = 40           # 嵌套创作/发布子流程上限（发帖成功前不占主预算）
    oscillation_window: int = 3          # 连续 N 步 (同 action + 同屏无变化) 判卡死
    phash_max_distance: int = 6          # 感知哈希汉明距离 ≤ 此值视为「屏幕几乎没变」
    recovery_enabled: bool = True        # L0 系统层恢复（YAML 规则驱动）
    max_recovery_rounds: int = 3         # 单条用例内恢复唤起轮数上限（代码兜的止损）
    recovery_stall_steps: int = 3        # 连续 N 步屏幕几乎没变 → 视为停滞，唤起一次恢复
    coord_tolerance_px: int = 48         # 落点相距 ≤此像素视为「同一个目标」（抗 VLM 坐标抖动）
    pause_ms_between_steps: int = 400
    step_timeout_sec: int = 90
    history_window: int = 8              # 喂给模型的最近步数
    capture_timeout_sec: float = 15.0
    hitl_timeout_sec: int = 300
    max_false_done: int = 2              # 判 done 但成功断言未过的最大容忍次数（超过判失败）
    restart_settle_sec: float = 2.0      # 重启后等应用起来


@dataclass
class _Step:
    idx: int
    thought: str = ""
    capability_id: str = ""
    params: dict = field(default_factory=dict)
    status: str = ""          # decision.status
    result_status: str = ""   # EventResult.status
    summary: str = ""
    screen_hash: str = ""     # 整图 sha1（trace 用，判定不用它）
    phash: str = ""           # 感知哈希（震荡判定用）


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _screen_hash(b64: str) -> str:
    return hashlib.sha1((b64 or "").encode("utf-8")).hexdigest()[:12] if b64 else ""


# 感知哈希：整图 sha1 太严（状态栏时钟、加载动画每帧都变 → 永远不相等），
# 导致震荡检测形同虚设（实测只能抓纯黑屏）。dHash 对"几乎没变"的屏幕给出相近值。
_PHASH_W, _PHASH_H = 9, 8          # 9x8 相邻比较 → 64 bit
_PHASH_CROP_TOP_PCT = 5            # 裁掉顶部状态栏（时钟/电量每分钟变）


@dataclass
class _ScreenSignal:
    """一次解码就同时算出感知哈希与「是否全黑/全白」，供震荡判定与恢复预筛共用。

    刻意合在一起：解码 + 灰度是主要开销，算两件事只多几行 numpy，
    这样 L0 的预筛是**零额外设备调用**的。
    """

    phash: str = ""
    blank: str = "unknown"     # no | black | white | unknown
    mean: float = -1.0


def _screen_signal(b64: str) -> _ScreenSignal:
    if not b64:
        return _ScreenSignal()
    try:
        import base64 as _b64
        from io import BytesIO

        import numpy as np
        from PIL import Image

        img = Image.open(BytesIO(_b64.b64decode(b64)))
        w, h = img.size
        if h > 20:
            img = img.crop((0, int(h * _PHASH_CROP_TOP_PCT / 100), w, h))
        gray = img.convert("L")
        full = np.asarray(gray, dtype=np.int16)
        mean = float(full.mean())
        std = float(full.std())
        # 阈值与 shared/screenshot/regression_capture.shot_is_blank 保持一致
        if mean <= 18.0 and std <= 14.0:
            blank = "black"
        elif mean >= 244.0 and std <= 14.0:
            blank = "white"
        else:
            blank = "no"

        small = np.asarray(gray.resize((_PHASH_W, _PHASH_H), Image.BILINEAR), dtype=np.int16)
        bits = (small[:, 1:] > small[:, :-1]).flatten()
        val = 0
        for b in bits:
            val = (val << 1) | int(b)
        return _ScreenSignal(phash=f"{val:016x}", blank=blank, mean=mean)
    except Exception as exc:  # pragma: no cover - 缺依赖/坏图时退化为未知
        SLog.w(TAG, f"screen signal failed: {exc}")
        return _ScreenSignal()


def _screen_phash(b64: str) -> str:
    """屏幕感知哈希（dHash，16 位十六进制）。失败返回空串=未知，不参与判定。"""
    return _screen_signal(b64).phash


def _phash_distance(a: str, b: str) -> int:
    """汉明距离；任一为空（未知）时返回 -1，调用方视为"无法判定"。"""
    if not a or not b:
        return -1
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return -1


def _count_numbered_in_text(text: str) -> int:
    """从步骤原文数编号。兼容「1.xxx 2.xxx」写在同一行。"""
    raw = (text or "").strip()
    if not raw:
        return 0
    nums = re.findall(r"(?:^|[\n;；\s])(\d+)[.、．)\）]", raw)
    if not nums:
        return 1
    try:
        return max(int(n) for n in nums)
    except ValueError:
        return len(nums)


def count_case_steps(case_spec: CaseSpec) -> int:
    """决策预算用的用例步骤数：优先飞书编号，其次 steps 条数。"""
    n_list = len(case_spec.steps or [])
    raw = ""
    row = case_spec.raw_row or {}
    if isinstance(row, dict):
        raw = str(row.get("steps_raw") or "")
    if not raw and case_spec.steps:
        raw = "\n".join(s.instruction for s in case_spec.steps if s.instruction)
    n_raw = _count_numbered_in_text(raw) if raw else 0
    return max(n_list, n_raw, 1)


def compute_decision_budget(case_spec: CaseSpec, opts: Optional[AgentOptions] = None) -> int:
    opts = opts or AgentOptions()
    n = count_case_steps(case_spec)
    return max(opts.min_steps, min(opts.max_steps_cap, n * opts.steps_per_case_step))


def case_text_blob(case_spec: CaseSpec) -> str:
    parts = [
        case_spec.name or "",
        case_spec.preconditions or "",
        case_spec.expected or "",
    ]
    for step in case_spec.steps or []:
        parts.append(step.instruction or "")
        parts.append(step.expected or "")
    raw = case_spec.raw_row or {}
    if isinstance(raw, dict):
        parts.append(str(raw.get("steps_raw") or ""))
        parts.append(str(raw.get("expected_raw") or ""))
    return "\n".join(parts)


def case_steps_text(case_spec: CaseSpec, *, max_chars: int = 1200) -> str:
    """用例操作步骤原文（优先 steps_raw），供知识库检索带上意图，不只靠 OCR。"""
    raw = case_spec.raw_row or {}
    text = ""
    if isinstance(raw, dict):
        text = str(raw.get("steps_raw") or "").strip()
    if not text:
        lines: list[str] = []
        for i, step in enumerate(case_spec.steps or [], 1):
            inst = str(getattr(step, "instruction", "") or "").strip()
            if inst:
                lines.append(f"{i}. {inst}")
        text = "\n".join(lines).strip()
    if not text:
        return ""
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "…"
    return text


def _clip_knowledge_bit(text: str, max_chars: int) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) > max_chars:
        return t[:max_chars].rstrip() + "…"
    return t


def build_case_intent_for_knowledge(
    *,
    case_name: str = "",
    goal: str = "",
    steps_text: str = "",
    preconditions: str = "",
    success_criteria: str = "",
    open_checkpoints: list[str] | None = None,
) -> str:
    """组装稳定的用例意图文本，保证入口类知识能靠步骤/目标命中。"""
    parts: list[str] = []
    for bit in (
        _clip_knowledge_bit(case_name, 200),
        _clip_knowledge_bit(goal, 400),
        _clip_knowledge_bit(steps_text, 1200),
        _clip_knowledge_bit(preconditions, 300),
    ):
        if bit:
            parts.append(bit)
    open_lines = [str(x).strip() for x in (open_checkpoints or []) if str(x).strip()]
    if open_lines:
        joined = "\n".join(open_lines)
        parts.append(_clip_knowledge_bit(joined, 600))
    sc = _clip_knowledge_bit(success_criteria, 300)
    if sc:
        parts.append(sc)
    return "\n".join(parts).strip()


def build_knowledge_query(
    *,
    case_intent: str = "",
    extra: str = "",
    last_action: str = "",
    history: str = "",
    screen: str = "",
) -> str:
    """知识库检索 query：用例意图优先，再叠本步动作/历史/屏幕文案。"""
    bits = [case_intent, extra, last_action, history, screen]
    return "\n".join(str(x).strip() for x in bits if str(x).strip())


def _knowledge_intent_tokens(text: str) -> list[str]:
    """从用例意图里抽出可对齐知识的关键短语（中英混合，无业务特判词表）。"""
    raw = (text or "").strip().lower()
    if not raw:
        return []
    found: list[str] = []
    # 引号内短语优先（常是控件/页面名）
    for m in re.finditer(r"[「\"'“](.{1,24})[」\"'”]", raw):
        t = m.group(1).strip()
        if len(t) >= 2 and t not in found:
            found.append(t)
    for tok in re.split(r"[\s,，、/\|;；:：\n\r\t.。！？\(\)（）\[\]【】]+", raw):
        t = tok.strip()
        if len(t) < 2 or t in found or t in _KNOWLEDGE_TOKEN_STOP:
            continue
        if re.fullmatch(r"[a-z0-9_\-]{3,}", t) or (len(t) >= 2 and re.search(r"[\u4e00-\u9fff]", t)):
            found.append(t[:24])
        if len(found) >= 24:
            break
    return found


def _distinctive_knowledge_tokens(text: str) -> set[str]:
    """从知识正文抽出可核对是否被决策引用的控件/路径词。"""
    raw = (text or "").strip().lower()
    out: set[str] = set()
    if not raw:
        return out
    for m in re.finditer(r"[「\"'“](.{1,24})[」\"'”]", raw):
        t = m.group(1).strip()
        if len(t) >= 2 and t not in _KNOWLEDGE_TOKEN_STOP:
            out.add(t)
    for tok in re.split(r"[\s,，、/\|;；:：\n\r\t.。！？\(\)（）\[\]【】\d]+", raw):
        t = tok.strip()
        if len(t) < 2 or t in _KNOWLEDGE_TOKEN_STOP:
            continue
        if re.fullmatch(r"[a-z0-9_\-]{3,}", t) or (len(t) >= 2 and re.search(r"[\u4e00-\u9fff]", t)):
            out.add(t[:24])
        if len(out) >= 40:
            break
    return out


def _knowledge_placeholder_body(body: str) -> bool:
    """失败沉淀但尚未补「正确操作方式」的空壳说明，不宜抢路径知识的位。"""
    text = body or ""
    if "【本应用正确操作方式】" not in text:
        return False
    after = text.split("【本应用正确操作方式】", 1)[-1]
    compact = re.sub(r"[\s\d\.、．]+", "", after)
    compact = compact.replace("（请按实际界面补充，保存后规划执行将自动匹配）", "")
    return len(compact) < 4


def is_path_knowledge_item(row: dict[str, Any]) -> bool:
    """是否像「怎么走/入口/操作路径」说明（通用启发式，不绑业务词）。"""
    blob = " ".join(
        str(x) for x in (
            row.get("title") or "",
            row.get("category") or "",
            " ".join(str(t) for t in (row.get("tags") or [])),
        )
    )
    if _PATH_KNOWLEDGE_RE.search(blob):
        return True
    cat = str(row.get("category") or "")
    return "导航" in cat or cat.lower() in {"ui导航", "navigation", "nav"}


def rank_knowledge_for_case_intent(
    rows: list[dict[str, Any]],
    *,
    case_intent: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """按与用例意图的重叠度重排；空壳失败笔记降权；路径类同重叠时优先。"""
    tokens = _knowledge_intent_tokens(case_intent)
    scored: list[tuple[tuple, dict[str, Any]]] = []
    for row in rows or []:
        title = str(row.get("title") or "")
        tags = " ".join(str(t) for t in (row.get("tags") or []))
        body = str(row.get("content") or row.get("prompt") or "")
        blob = f"{title} {tags} {body[:400]}".lower()
        overlap = sum(1 for t in tokens if t and t in blob)
        placeholder = 1 if _knowledge_placeholder_body(body) else 0
        used = 1 if row.get("used") else 0
        pathish = 1 if is_path_knowledge_item(row) else 0
        pct = int(row.get("match_pct") or 0)
        score = int(row.get("score") or 0)
        # used → 意图重叠 → 路径类 → 非空壳 → 原始匹配分
        key = (used, overlap, pathish, 0 if placeholder else 1, pct, score)
        scored.append((key, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[: max(1, int(limit or 3))]]


def build_knowledge_index_text(rows: list[dict[str, Any]]) -> str:
    """组装知识目录：只含 id/标题/时机，不含正文。"""
    lines: list[str] = []
    for r in rows or []:
        kid = str(r.get("id") or "").strip()
        title = str(r.get("title") or "").strip()
        if not kid or not title:
            continue
        if r.get("used") is False:
            continue
        when = str(r.get("when") or "").strip() or " ".join(
            str(t) for t in (r.get("tags") or []) if t
        )
        cat = str(r.get("category") or "").strip()
        bit = f"- {kid} 「{title}」"
        if cat:
            bit += f" [{cat}]"
        if when:
            bit += f" when={when}"
        lines.append(bit)
    if not lines:
        return "（本步无已审核知识可点名）"
    return "不点名则本步不展开正文。目录：\n" + "\n".join(lines)


def build_knowledge_hint_text(
    rows: list[dict[str, Any]],
    *,
    case_intent: str = "",
) -> str:
    """组装注入 decide 的知识块：路径类前置；文案不含业务特判。

    case_intent 保留参数以兼容调用方；本步 used 条目已由检索+重排筛过，路径类一律前置。
    """
    del case_intent  # 检索阶段已用意图对齐；此处只做路径/非路径分桶
    used = [r for r in (rows or []) if r.get("used") and r.get("prompt")]
    if not used:
        return ""
    primary: list[str] = []
    secondary: list[str] = []
    for r in used:
        prompt = str(r.get("prompt") or "")
        if is_path_knowledge_item(r):
            primary.append(prompt)
        else:
            secondary.append(prompt)
    parts: list[str] = []
    if primary:
        parts.append(
            "【优先执行·操作路径】下列知识写明了与当前目标/检查点相关的入口或步骤，"
            "请按知识中的控件与路径执行；不要改用知识未写明的替代入口。"
        )
        parts.extend(primary)
    if secondary:
        if primary:
            parts.append("【其他相关参考】")
        parts.extend(secondary)
    if not parts:
        parts = [str(r.get("prompt") or "") for r in used if r.get("prompt")]
    return "\n".join(p for p in parts if p).strip()


def build_path_knowledge_nudge(
    decision,
    rows: list[dict[str, Any]],
    *,
    case_intent: str = "",
) -> str:
    """通用纠正：已命中路径类知识，但决策未引用知识中的关键控件/路径词。"""
    del case_intent  # 本步 used 路径知识已由检索对齐意图
    path_rows = [
        r for r in (rows or [])
        if r.get("used") is not False and is_path_knowledge_item(r) and r.get("id")
    ]
    if not path_rows:
        return ""
    named = {str(x).strip() for x in (getattr(decision, "knowledge_ids", None) or []) if str(x).strip()}
    if any(str(r.get("id") or "") in named for r in path_rows):
        return ""
    thought = f"{getattr(decision, 'thought', '') or ''} {getattr(decision, 'expected_after', '') or ''}"
    if not _NAV_ACT_RE.search(thought):
        return ""
    bits = [f"{r.get('id')}「{r.get('title') or ''}」" for r in path_rows[:3]]
    return (
        "【纠正】目录中有路径类知识 " + "、".join(bits)
        + "，你在选入口但未点名。若要按该路径走，把 knowledge_ids 设为对应 id；"
        "不要改用知识未写明的替代入口（仅当与当前屏幕明显冲突时可偏离）。"
    )


def case_needs_nested_publish(case_spec: CaseSpec, extra: str = "") -> bool:
    """步骤里嵌「再发一条」整段创作发布时，发帖成功前不占用主决策预算。"""
    blob = case_text_blob(case_spec) + "\n" + (extra or "")
    return bool(_NESTED_PUBLISH_RE.search(blob))


class AgentExecutor:
    def __init__(
        self,
        *,
        goal: CaseGoal,
        run_context: RunContext,
        router: CapabilityRouter,
        run_id: str = "",
        case_id: str = "",
        case_brief: str = "",
        provider_id: Optional[str] = None,
        options: Optional[AgentOptions] = None,
        case_preconditions: str = "",
        case_name: str = "",
        case_steps_text: str = "",
        nested_publish: bool = False,
        knowledge_hits: list[dict[str, Any]] | None = None,
    ):
        self.goal = goal
        self.ctx = run_context
        self.router = router
        self.run_id = run_id or f"agent-{int(time.time())}"
        self.case_id = case_id or goal.case_id
        self.case_brief = case_brief or goal.goal
        self.provider_id = provider_id
        self.opts = options or AgentOptions()
        self.case_preconditions = case_preconditions or ""
        self.case_name = case_name or ""
        self.case_steps_text = case_steps_text or ""
        self._nested_publish = bool(nested_publish)
        self.knowledge_hits = knowledge_hits or []
        self.shared: dict[str, Any] = {}
        self._assert_feedback = ""   # 上次"判 done 但校验未过"的理由，回灌给下一步
        self._false_done = 0
        self.steps: list[_Step] = []
        self.results: list[EventResult] = []
        self._decision_used = 0
        self._wait_rounds = 0
        self._create_used = 0
        self._published = False
        self._memory: list[tuple[str, str]] = []  # (kind, text)
        self._last_phash = ""    # 上一步屏幕感知哈希（供停滞判定 / L0 预筛复用）
        self._stall_steps = 0    # 连续「屏幕几乎没变」的步数
        self._recovery_rounds = 0  # 本条用例已唤起恢复的轮数
        self._recovery_hits: list[dict[str, Any]] = []  # 命中过的规则，进报告
        self._checked_at_start = False  # 开场是否做过一次恢复检查
        self._kb_path_retried: set[int] = set()  # 已对「忽略路径知识」做过纠正重决的步号
        self._kb_expanded: set[int] = set()
        self._recovery_advice = ""
        self._oscillation_advice = ""
        self._static_repeat = 0  # 已跳过重复点击并改做后续；顺带抑制 L0 停滞恢复
        self._followup_after_repeat = False  # 已经把「后面的操作」跑过一次
        self._session_note = ""
        self._session_inspected = False

    # ---------- prompt 片段 ----------

    def _checkpoints_block(self) -> str:
        if not self.goal.checkpoints:
            return "（无显式检查点，按目标自行判断进度）"
        return "\n".join(
            f"[{'x' if cp.done else ' '}] {cp.id}({ '过程' if getattr(cp, 'kind', 'terminal') == 'process' else '终态' }): {cp.description}"
            for cp in self.goal.checkpoints
        )

    def _history_block(self) -> str:
        """只保留「最近做过什么、结果如何」，不回灌 thought 和完整 params。"""
        picked: list[_Step] = []
        for step in reversed(self.steps):
            if str(step.capability_id or "") in _HISTORY_NOISE_CAPS:
                continue
            picked.append(step)
            if len(picked) >= self.opts.history_window:
                break
        picked.reverse()
        lines: list[str] = []
        for step in picked:
            status = str(step.result_status or step.status or "").split(".")[-1].lower() or "?"
            action = history_action_brief(step.capability_id, step.params) if step.capability_id else status
            line = f"{step.idx} {status} {action}"
            note = _clip_hist(step.summary, 36)
            if note:
                line += f" · {note}"
            lines.append(line)
        extras: list[str] = []
        ans = self.shared.get("hitl_last_answer")
        if ans:
            src = "号池" if str(ans.get("source") or "") == "account_pool" else "人工"
            extras.append(f"[{src}回复] {_clip_hist(ans.get('answer'), 40)}")
        if self._assert_feedback:
            extras.append(f"[校验未通过] {_clip_hist(self._assert_feedback, 80)}")
        left = max(0, self.opts.max_steps - self._decision_used)
        if self._in_create_flow():
            extras.append(
                f"[预算] 创作/发布进行中，本步不占决策"
                f"（{self._create_used}/{self.opts.max_create_steps}）；剩余 {left}/{self.opts.max_steps}"
            )
        else:
            extras.append(f"[预算] 剩余 {left}/{self.opts.max_steps}")
        body = "\n".join(lines)
        if len(body) > _HISTORY_BLOCK_MAX_CHARS:
            body = body[-_HISTORY_BLOCK_MAX_CHARS:]
            cut = body.find("\n")
            if cut > 0:
                body = body[cut + 1 :]
        return "\n".join(x for x in (body, *extras) if x)

    def _memory_block(self) -> str:
        if not self._memory:
            return ""
        labels = {"published": "发布", "before": "之前", "fact": "记住", "observed": "观察"}
        return "\n".join(f"- [{labels.get(kind, kind)}] {text}" for kind, text in self._memory)

    def _case_intent_for_knowledge(self) -> str:
        open_cps = [
            str(cp.description or "").strip()
            for cp in (self.goal.checkpoints or [])
            if not getattr(cp, "done", False) and str(cp.description or "").strip()
        ]
        return build_case_intent_for_knowledge(
            case_name=self.case_name,
            goal=self.goal.goal or self.case_brief,
            steps_text=self.case_steps_text,
            preconditions=self.case_preconditions,
            success_criteria=self.goal.success_criteria or "",
            open_checkpoints=open_cps,
        )

    def _knowledge_query(self, *, extra: str = "", dump=None) -> str:
        """本步检索上下文：用例意图（目标/步骤/未完成检查点）+ 最近动作 + 屏幕文案。

        入口类知识（如「如何进入 Agent / 对话历史」）往往不出现在 OCR 里；
        只靠屏幕会误匹配首页 feed 等无关说明，因此必须把用例意图稳定带进 query。
        """
        screen = ""
        if dump is not None:
            try:
                from server.services.regression.hierarchy import to_prompt_text
                screen = to_prompt_text(dump, limit=80, max_chars=1600)
            except Exception:
                screen = ""
        last = ""
        if self.steps:
            s = self.steps[-1]
            last = " ".join(x for x in (
                s.capability_id, s.thought or "", s.summary or "",
            ) if x)
        hist = ""
        try:
            hist = (self._history_block() or "")[-500:]
        except Exception:
            hist = ""
        return build_knowledge_query(
            case_intent=self._case_intent_for_knowledge(),
            extra=extra,
            last_action=last,
            history=hist,
            screen=screen,
        )

    def _knowledge_rows_from_hits(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from server.services.system_settings_service import (
            knowledge_body_text,
            knowledge_prompt_snippet,
        )
        rows: list[dict[str, Any]] = []
        for item in hits or []:
            title = str(item.get("title") or "").strip()
            kid = str(item.get("id") or "").strip()
            if not title:
                continue
            body = knowledge_body_text(item)
            used = item.get("used") is not False and bool(body)
            tags = item.get("tags") or []
            when = " ".join(str(t) for t in tags if t)
            rows.append({
                "uid": f"learned/knowledge/{kid}" if kid else "",
                "id": kid,
                "title": title,
                "category": str(item.get("category") or ""),
                "tags": tags,
                "when": when,
                "content": body[:2000],
                "score": int(item.get("score") or 0),
                "match_pct": int(item.get("match_pct") or 0),
                "used": used,
                "skip_reason": str(item.get("skip_reason") or ""),
                "prompt": knowledge_prompt_snippet(item) if used else "",
            })
        return rows

    def _query_knowledge(
        self,
        *,
        extra: str = "",
        dump=None,
        limit: int = 3,
        categories: list[str] | None = None,
        exclude_categories: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        from server.services.system_settings_service import (
            dedupe_knowledge_hits,
            match_testing_knowledge,
        )
        query = self._knowledge_query(extra=extra, dump=dump)
        try:
            hits = match_testing_knowledge(
                query,
                app_id=str(getattr(self.ctx, "app_id", "") or ""),
                limit=limit,
                categories=categories,
                exclude_categories=exclude_categories,
            )
        except Exception as exc:
            SLog.w(TAG, f"[{self.run_id}] step knowledge match failed: {type(exc).__name__}: {exc}")
            return []
        # 全库按得分已截到 limit；此处只对这 N 条按用例意图重排，不再扩配额
        return dedupe_knowledge_hits(rank_knowledge_for_case_intent(
            dedupe_knowledge_hits(self._knowledge_rows_from_hits(hits)),
            case_intent=self._case_intent_for_knowledge(),
            limit=limit,
        ))

    def _match_step_knowledge(self, *, extra: str = "", dump=None) -> list[dict[str, Any]]:
        ranked = self._query_knowledge(extra=extra, dump=dump, limit=12)
        self.knowledge_hits = ranked
        return ranked

    def _knowledge_hint(self, rows: list[dict[str, Any]]) -> str:
        return build_knowledge_index_text(rows)

    def _compose_knowledge_hint(self, rows: list[dict[str, Any]], *, body: str = "") -> str:
        parts = [self._knowledge_hint(rows)]
        if self._recovery_advice:
            parts.append("【系统框建议】" + self._recovery_advice)
        if self._oscillation_advice:
            parts.append(self._oscillation_advice)
        if body.strip():
            parts.append("==== 你点名的知识正文（仅本步）====\n" + body.strip()[:800])
        return "\n\n".join(p for p in parts if p).strip()

    def _expand_named_knowledge(self, decision, index_rows: list[dict[str, Any]]) -> str:
        ids = [str(x).strip() for x in (getattr(decision, "knowledge_ids", None) or []) if str(x).strip()]
        if not ids:
            return ""
        allowed = {str(r.get("id") or "") for r in (index_rows or []) if r.get("id")}
        ids = [i for i in ids if i in allowed]
        if not ids:
            return ""
        from server.services.system_settings_service import (
            get_knowledge_items_by_ids,
            knowledge_prompt_snippet,
        )
        items = get_knowledge_items_by_ids(
            ids, app_id=str(getattr(self.ctx, "app_id", "") or ""),
        )
        if not items:
            return ""
        snippets = [knowledge_prompt_snippet(it, max_chars=800) for it in items[:1]]
        return "\n".join(s for s in snippets if s).strip()

    def _decide(self, screen, *, knowledge_hint: str):
        return planner.decide_next_action(
            goal=self.goal.goal,
            success_criteria=self.goal.success_criteria,
            checkpoints_block=self._checkpoints_block(),
            run_context=self.ctx,
            history_block=self._history_block(),
            width=screen.width, height=screen.height,
            image_base64=screen.image_base64, image_mime=screen.image_mime,
            knowledge_hint=knowledge_hint,
            memory_block=self._memory_block(),
            session_block=self._session_block_text(),
            provider_id=self.provider_id, timeout_sec=self.opts.step_timeout_sec,
        )

    def _path_knowledge_nudge(
        self,
        decision,
        rows: list[dict[str, Any]],
    ) -> str:
        """路径类知识已命中但决策未引用知识控件时，生成通用纠正提示。"""
        return build_path_knowledge_nudge(
            decision,
            rows,
            case_intent=self._case_intent_for_knowledge(),
        )

    def _screen_dump(self):
        try:
            from server.services.regression import hierarchy as H
            serial = str(getattr(self.ctx, "sn", "") or "")
            if serial:
                return H.dump_ui_nodes(serial, force_fresh=False)
        except Exception as exc:
            SLog.d(TAG, f"[{self.run_id}] knowledge dump failed: {exc}")
        return None

    def _emit(self, phase: str, *, step: int = 0, thumb: str = "", decision=None,
              result_status: str = "", summary: str = "", overall: str = "",
              failure_category: str = "", failure_label: str = "",
              elapsed_ms: int | None = None, capability_id: str = "",
              packs: list | None = None, recovery: dict | None = None,
              knowledge: list[dict[str, Any]] | None = None):
        data: dict[str, Any] = {
            "run_id": self.run_id, "case_id": self.case_id, "sn": self.ctx.sn or "",
            "phase": phase, "step": step, "goal": self.goal.goal,
        }
        if phase == "start":
            data["checkpoints"] = [
                {
                    "id": c.id,
                    "description": c.description,
                    "kind": getattr(c, "kind", "terminal") or "terminal",
                    "done": bool(getattr(c, "done", False)),
                }
                for c in self.goal.checkpoints
            ]
        if decision is not None:
            data["thought"] = decision.thought
            data["status"] = decision.status
            data["expected_after"] = decision.expected_after
            data["action"] = (
                {"capability_id": decision.action.capability_id, "params": decision.action.params}
                if decision.action else None
            )
            # agent 决策辅助信息：用于 UI 溯源（checkpoint 命中 / 记忆 / prompt 输出等）
            if getattr(decision, "knowledge_ids", None) is not None:
                data["knowledge_ids"] = list(decision.knowledge_ids or [])
            if getattr(decision, "checkpoint_ids", None) is not None:
                ids = list(decision.checkpoint_ids or [])
                data["checkpoint_ids"] = ids
                by_id = {c.id: c for c in (self.goal.checkpoints or [])}
                data["checkpoints_hit"] = [
                    {
                        "id": cid,
                        "description": str(getattr(by_id.get(cid), "description", "") or ""),
                        "kind": str(getattr(by_id.get(cid), "kind", "") or ""),
                    }
                    for cid in ids
                ]
            if getattr(decision, "remember", None) is not None:
                data["remember"] = list(decision.remember or [])
            if getattr(decision, "subflow", None) is not None:
                data["subflow"] = decision.subflow
            if getattr(decision, "published", None) is not None:
                data["published"] = decision.published
            if getattr(decision, "confidence", None) is not None:
                data["confidence"] = float(decision.confidence or 0.0)
            if getattr(decision, "parse_warnings", None) is not None:
                data["parse_warnings"] = list(decision.parse_warnings or [])

            raw_llm = getattr(decision, "raw_llm", None)
            if isinstance(raw_llm, dict):
                # 由 planner 在 debug 模式注入：input/output/meta
                if "llm_input" in raw_llm:
                    data["llm_input"] = raw_llm.get("llm_input")
                if "llm_output" in raw_llm:
                    data["llm_output"] = raw_llm.get("llm_output")
                if "meta" in raw_llm:
                    data["llm_meta"] = raw_llm.get("meta")
        if thumb:
            data["thumb"] = thumb
        if result_status:
            data["result_status"] = result_status
        if summary:
            data["summary"] = summary[:200]
        if overall:
            data["overall"] = overall
        if failure_category:
            data["failure_category"] = failure_category
        if failure_label:
            data["failure_label"] = failure_label
        if elapsed_ms is not None:
            data["elapsed_ms"] = int(elapsed_ms)
        if capability_id:
            data["capability_id"] = capability_id
        # 本步命中/注入了哪些 pack（回放页据此显示「本步命中」栏）
        if packs is not None:
            data["packs"] = packs
        if recovery is not None:
            data["recovery"] = recovery
        if knowledge:
            data["knowledge"] = [
                {
                    "uid": r.get("uid") or "",
                    "id": r.get("id") or "",
                    "title": r.get("title") or "",
                    "category": r.get("category") or "",
                    "tags": r.get("tags") or [],
                    "when": r.get("when") or "",
                    "used": r.get("used"),
                    "match_pct": r.get("match_pct"),
                    "skip_reason": r.get("skip_reason") or "",
                }
                for r in knowledge
                if r.get("id") and r.get("title")
            ]
        agent_stream.emit_agent_event(data)

    # ---------- 主循环 ----------

    def run(self) -> RunReport:
        started_ts = time.time()
        started_at = _now_iso()
        overall = "fail"
        blocked_reason = ""
        decline_reason = ""
        failure_category = ""   # success | goal_unreachable | execution_error | budget_exhausted | needs_human
        SLog.i(TAG, f"[{self.run_id}] >>> agent case={self.case_id} goal={self.goal.goal!r} "
                    f"checkpoints={len(self.goal.checkpoints)} decision_budget={self.opts.max_steps}")
        self._emit("start")
        self._note_issued_account()
        self._maybe_bootstrap_restart()
        gated = None
        try:
            gated = self._gate_session_before_loop()
        except Exception as exc:
            SLog.w(TAG, f"[{self.run_id}] session gate crashed: {exc}")
            gated = None
        if gated:
            overall, decline_reason, failure_category = gated
        capture_fails = 0
        while (not gated) and self._decision_used < self.opts.max_steps:
            if self._task_cancelled():
                overall = "fail"
                decline_reason = "任务已取消"
                failure_category = "execution_error"
                break
            step_idx = len(self.results) + 1
            screen = capture_screen(
                self.ctx, prefer=self.router.capture_prefer,
                timeout_sec=self.opts.capture_timeout_sec, force_fresh=True,
            )
            if not screen.has_image():
                capture_fails += 1
                SLog.w(TAG, f"[{self.run_id}] step{step_idx} 截图失败: {screen.error}")
                self._record_synthetic(step_idx, EventStatus.FAIL, "capture_screen", f"截图失败: {screen.error}")
                if capture_fails >= 2:
                    decline_reason = f"截图失败: {screen.error}"
                    failure_category = "execution_error"
                    break
                time.sleep(1.0)
                continue
            capture_fails = 0

            shot_hash = _screen_hash(screen.image_base64)
            signal = _screen_signal(screen.image_base64)
            shot_phash = signal.phash
            self._track_stall(shot_phash)
            self._last_phash = shot_phash
            thumb = agent_stream.make_thumb(screen.image_base64)

            # ---- L0 系统层恢复：预筛（零设备调用）→ 命中规则则处置 → 重新观察 ----
            rec = self._maybe_recover(signal, step_idx, thumb)
            if rec is not None:
                if rec.get("fatal"):
                    decline_reason = rec.get("reason") or "系统层无法恢复"
                    failure_category = rec["fatal"]
                    break
                if rec.get("recovered"):
                    continue      # 屏幕已变，重新截图再决策；本步不计业务预算

            dump = self._screen_dump()
            shown_knowledge = self._match_step_knowledge(dump=dump)
            knowledge_hint = self._compose_knowledge_hint(shown_knowledge)
            self._maybe_inspect_session(screen, knowledge=shown_knowledge)
            step_idx = len(self.results) + 1
            self._emit(
                "think",
                step=step_idx,
                thumb=thumb,
                summary="正在看图决策…",
                knowledge=shown_knowledge,
            )
            decision = self._decide(screen, knowledge_hint=knowledge_hint)
            expanded = ""
            if step_idx not in self._kb_expanded:
                expanded = self._expand_named_knowledge(decision, shown_knowledge)
                if expanded:
                    self._kb_expanded.add(step_idx)
                    SLog.i(TAG, f"[{self.run_id}] step{step_idx} expand knowledge_ids={decision.knowledge_ids}")
                    decision = self._decide(
                        screen,
                        knowledge_hint=self._compose_knowledge_hint(shown_knowledge, body=expanded),
                    )
            self._recovery_advice = ""
            self._oscillation_advice = ""
            nudge = self._path_knowledge_nudge(decision, shown_knowledge)
            if (not expanded) and nudge and step_idx not in self._kb_path_retried:
                self._kb_path_retried.add(step_idx)
                SLog.w(
                    TAG,
                    f"[{self.run_id}] step{step_idx} 决策未点名路径知识，纠正重决一次",
                )
                decision = self._decide(
                    screen,
                    knowledge_hint=f"{knowledge_hint}\n\n{nudge}".strip(),
                )
                if step_idx not in self._kb_expanded:
                    expanded = self._expand_named_knowledge(decision, shown_knowledge)
                    if expanded:
                        self._kb_expanded.add(step_idx)
                        decision = self._decide(
                            screen,
                            knowledge_hint=self._compose_knowledge_hint(shown_knowledge, body=expanded),
                        )
            cap = decision.action.capability_id if decision.action else ""
            self._ingest_decision_memory(decision, cap)
            SLog.i(TAG, f"[{self.run_id}] step{step_idx} status={decision.status} "
                        f"act={cap or '-'} decision={self._decision_used}/{self.opts.max_steps} "
                        f"thought={decision.thought[:80]!r}")
            self._emit(
                "step",
                step=step_idx,
                thumb=thumb,
                decision=decision,
                knowledge=shown_knowledge,
            )

            # ---- done：用成功标准断言；不通过则回灌理由继续，不立即失败（弥合 done/assert 分裂） ----
            if decision.status == "done":
                ok, reason = self._assert_goal(screen, step_idx)
                self._count_decision()
                if ok:
                    overall = "pass"
                    failure_category = "success"
                    break
                self._false_done += 1
                self._assert_feedback = reason
                SLog.w(TAG, f"[{self.run_id}] done 但校验未过({self._false_done}/{self.opts.max_false_done}): {reason[:80]}")
                if self._false_done >= self.opts.max_false_done:
                    overall = "fail"
                    failure_category = "execution_error"
                    decline_reason = f"多次判定完成但成功标准始终未在屏幕出现：{reason[:200]}"
                    break
                time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                continue

            # ---- give_up：agent 判定客观无法完成（如应用无此功能/环境不符） ----
            if decision.status == "give_up":
                self._record_synthetic(step_idx, EventStatus.FAIL, "give_up", decision.thought[:200], shot_hash, shot_phash)
                self._count_decision()
                decline_reason = decision.thought[:240] or "agent give_up"
                overall = "fail"
                llm_broke = "llm failed" in (decision.parse_warnings or [])
                failure_category = "execution_error" if llm_broke else "goal_unreachable"
                break

            # ---- ask_human ----
            if decision.status == "ask_human":
                res = self._ask_human(decision, step_idx, shot_hash, shot_phash)
                self._count_decision()
                if res == "blocked":
                    overall = "blocked"
                    blocked_reason = "人工未在时限内回复"
                    failure_category = "needs_human"
                    break
                if res == "give_up":
                    overall = "fail"
                    decline_reason = (
                        (self.results[-1].summary if self.results else "")
                        or "需要人工时只能提供信息，不能改为让人在设备上操作"
                    )
                    failure_category = "goal_unreachable"
                    break
                time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                continue

            # ---- continue：执行一个动作 ----
            if decision.action is None or not cap:
                self._record_synthetic(step_idx, EventStatus.FAIL, "noop", "continue 但无有效 action", shot_hash, shot_phash)
                self._count_decision()
                if self._is_oscillating():
                    decline_reason = "连续无有效动作"
                    failure_category = "execution_error"
                    break
                continue

            guard = self._env_manufacture_reason(decision, cap)
            if guard:
                self._record_synthetic(step_idx, EventStatus.FAIL, "give_up", guard, shot_hash, shot_phash)
                self._count_decision()
                decline_reason = guard
                overall = "fail"
                failure_category = "goal_unreachable"
                break

            is_wait = cap in _WAIT_CAPS
            event_params = self._normalize_action_params(cap, dict(decision.action.params or {}))
            # 同一入口再点：不看页面有没有切，跳过这次点击，改做后面的检查点/成功标准。
            if cap == "tap_element" and self._repeats_last_mutate(cap, event_params):
                SLog.w(TAG, f"[{self.run_id}] 同一入口再点，跳过点击，不检测页面切换")
                self._record_synthetic(
                    step_idx, EventStatus.SKIPPED, "skip_repeat_tap",
                    "入口已点过，不检测页面是否切换，改为后续操作",
                    shot_hash, shot_phash, thumb=thumb,
                )
                self._count_decision()
                self._static_repeat = 1
                self._oscillation_advice = _PROCEED_AFTER_ENTRY
                self._remember("fact", "入口已点过，不检测页面是否切换，改为后续操作")
                if self._followup_after_repeat:
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
                self._followup_after_repeat = True
                self._skip_nav_verify_checkpoints()
                nxt = self._next_undone_checkpoint()
                if nxt is None:
                    ok, reason = self._assert_goal(screen, len(self.results) + 1)
                    if ok:
                        overall = "pass"
                        failure_category = "success"
                        break
                    self._assert_feedback = reason
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
                cap = "assert_visual"
                is_wait = False
                event_params = {"expectation": nxt.description}
                decision.action = AgentAction(capability_id=cap, params=dict(event_params))
                decision.checkpoint_ids = [nxt.id]
                decision.thought = (
                    f"入口已点过，不检测页面是否切换，改为验证：{nxt.description}"
                )
                step_idx = len(self.results) + 1
            if cap == "assert_visual":
                mem = self._memory_block()
                if mem:
                    event_params["memory_context"] = (
                        "==== 短期记忆（当前截图是现在）====\n" + mem
                    )
                if not event_params.get("expectation") and decision.expected_after:
                    event_params["expectation"] = decision.expected_after
            event = PlanEvent(
                seq=step_idx,
                capability_id=cap,
                event_kind=cap,
                params=event_params,
                needs_vlm=False,  # D2：坐标已由决策 VLM 给出，不再走 locate VLM
                expected_executor="",  # 让 router 按连通性+cost 自选（adb 优先）
                ai_reasoning=decision.thought[:240] or "(agent)",
                label=decision.expected_after[:120],
            )
            result = self.router.dispatch(
                event, run_id=self.run_id, case_id=self.case_id,
                case_brief=self.case_brief, shared=self.shared,
            )
            if thumb and not getattr(result, "thumb", None):
                try:
                    result = result.model_copy(update={"thumb": thumb})
                except Exception:
                    pass
            if shown_knowledge:
                try:
                    result = result.model_copy(update={"knowledge": list(shown_knowledge)})
                except Exception:
                    pass
            self.results.append(result)
            self._push_step(step_idx, decision, result_status=str(result.status.value),
                            summary=result.summary or result.error, screen_hash=shot_hash,
                            phash=shot_phash)
            self._emit(
                "result",
                step=step_idx,
                result_status=str(result.status.value),
                summary=result.summary or result.error,
                elapsed_ms=int(getattr(result, "elapsed_ms", 0) or 0),
                knowledge=shown_knowledge,
            )
            # 有实际动作推进 → 清掉上次的 done 反馈
            self._assert_feedback = ""

            if is_wait:
                self._wait_rounds += 1
                if self._wait_rounds >= self.opts.max_wait_rounds:
                    decline_reason = f"连续等待 {self._wait_rounds} 次仍未就绪"
                    overall = "fail"
                    failure_category = "execution_error"
                    break
            elif self._in_create_flow():
                self._create_used += 1
                self._wait_rounds = 0
                if self._create_used >= self.opts.max_create_steps:
                    decline_reason = (
                        f"创作/发布子流程达到上限 {self.opts.max_create_steps} 仍未发布成功"
                    )
                    overall = "fail"
                    failure_category = "budget_exhausted"
                    break
            else:
                self._count_decision()

            if cap == "assert_visual" and result.status == EventStatus.PASS:
                if result.summary:
                    self._remember("observed", result.summary[:180])
                if decision.checkpoint_ids:
                    self._mark_checkpoints(list(decision.checkpoint_ids))
                if not decision.checkpoint_ids:
                    blob = (
                        (result.summary or "")
                        + (decision.expected_after or "")
                        + str((decision.action.params or {}).get("expectation") or "")
                    )
                    if _PROCESS_HINT_RE.search(blob):
                        self._mark_next_process_checkpoint()

            if result.status == EventStatus.BLOCKED:
                overall = "blocked"
                blocked_reason = result.error or "executor blocked"
                failure_category = "needs_human"
                break

            # 坐标抖动导致同一入口连点仍被 dispatch 时：不要停跑，也不要去验页面切没切。
            if cap in _MUTATE_CAPS and self._is_oscillating():
                self._static_repeat = 1
                self._oscillation_advice = _PROCEED_AFTER_ENTRY
                self._remember("fact", "入口已点过，不检测页面是否切换，改为后续操作")

            time.sleep(self.opts.pause_ms_between_steps / 1000.0)
        else:
            decline_reason = (
                f"达到决策步上限 max_steps={self.opts.max_steps} 仍未完成目标"
                f"（wait 未计入，已决策 {self._decision_used}）"
            )
            overall = "partial"
            failure_category = "budget_exhausted"

        return self._build_report(overall, started_at, started_ts, blocked_reason, decline_reason, failure_category)

    # ---------- L0 系统层恢复（YAML 规则驱动，见 plugins/recovery/） ----------

    def _track_stall(self, phash: str) -> None:
        """连续「屏幕几乎没变」计数。用已算好的 phash，不额外碰设备。"""
        d = _phash_distance(self._last_phash, phash)
        if d >= 0 and d <= self.opts.phash_max_distance:
            self._stall_steps += 1
        else:
            self._stall_steps = 0

    def _recovery_suspicion(self, signal: "_ScreenSignal") -> str:
        """廉价预筛：这一屏值不值得花一次取证去查规则。命中返回原因，否则空串。

        判据刻意只用**已经算出来的东西**（画面统计 + 停滞计数），
        所以正常屏每步的额外开销是 0。
        """
        if not self.opts.recovery_enabled:
            return ""
        if self._recovery_rounds >= self.opts.max_recovery_rounds:
            return ""
        if not self._checked_at_start:
            # 开场查一次：实测最常见的事故就是「设备过夜息屏，用例一上来就全挂」
            return "case_start"
        if signal.blank in ("black", "white"):
            return f"blank_{signal.blank}"
        if self._stall_steps >= self.opts.recovery_stall_steps:
            # 已经在「同入口连点屏幕未变」里打转时，不要再当 L0 停滞去 dump + 恢复，
            # 那只会多一轮看图，拦不住重复点击。
            if self._static_repeat:
                return ""
            return f"stalled_{self._stall_steps}"
        return ""

    def _maybe_recover(self, signal: "_ScreenSignal", step_idx: int, thumb: str):
        """预筛命中就跑一次恢复。返回 None 表示没做任何事。

        返回 dict：{recovered: bool, fatal: str, reason: str, packs: [...]}
        """
        reason = self._recovery_suspicion(signal)
        self._checked_at_start = True
        if not reason:
            return None

        from server.services.regression import recovery as R

        self._recovery_rounds += 1
        pkg = str(getattr(self.ctx, "target_package", "") or "")
        # 取一次 UI 层级给 recovery 做匹配：依赖 screen_text_any 的 system dialog 规则需要它。
        # 只有在预筛命中 recovery 时才会走到这里，因此成本是可控的。
        dump = None
        try:
            from server.services.regression import hierarchy as H

            serial = str(getattr(self.ctx, "sn", "") or "")
            if serial:
                dump = H.dump_ui_nodes(serial, force_fresh=True)
        except Exception as exc:  # pragma: no cover - recovery 不应拖垮主流程
            SLog.w(TAG, f"[{self.run_id}] hierarchy dump 供 recovery 失败: {exc}")
        try:
            outcome = R.recover_if_needed(
                self.ctx, self.router, target_package=pkg, dump=dump
            )
        except Exception as exc:  # pragma: no cover - 恢复本身不该拖垮用例
            SLog.w(TAG, f"[{self.run_id}] 恢复流程异常: {exc}")
            return None

        if outcome is None:
            SLog.i(TAG, f"[{self.run_id}] 预筛({reason})无规则命中，交给业务决策")
            k = self._match_step_knowledge(extra=f"{reason} 无规则命中", dump=dump)
            self._emit("recovery", step=step_idx, thumb=thumb,
                       summary=f"预筛({reason})：无规则命中",
                       packs=[], recovery={"trigger": reason, "matched": False},
                       knowledge=k)
            return None

        if getattr(self.ctx, "keep_permission_prompt", False) and str(
            outcome.rule_id or ""
        ).startswith("system_permission"):
            SLog.i(TAG, f"[{self.run_id}] keep_permission_prompt skip {outcome.rule_id}")
            return None

        pack = {
            "uid": f"builtin/recovery/{outcome.rule_id}",
            "kind": "recovery",
            "id": outcome.rule_id,
            "mode": outcome.mode,
            "matched": True,
            "applied": outcome.applied,
            "recovered": outcome.recovered,
            "trigger": reason,
        }
        self._recovery_hits.append(pack)
        summary = f"[{reason}] {outcome.summary()}"
        rec_payload = {
            "trigger": reason, "matched": True, "rule_id": outcome.rule_id,
            "mode": outcome.mode, "recovered": outcome.recovered,
            "actions": outcome.actions, "advice": (outcome.advice or "")[:400],
            "error": outcome.error,
        }
        self._record_synthetic(
            step_idx,
            EventStatus.PASS if (outcome.recovered or outcome.mode == "advise") else EventStatus.FAIL,
            f"recovery_{outcome.rule_id}", summary, "", signal.phash,
            recovery=rec_payload, thumb=thumb,
        )
        k = self._match_step_knowledge(extra=summary, dump=dump)
        # 恢复动作不占业务决策预算，但要能在回放里看见
        self._emit("recovery", step=step_idx, thumb=thumb, summary=summary,
                   packs=[pack], recovery=rec_payload, knowledge=k)
        SLog.i(TAG, f"[{self.run_id}] 恢复 {outcome.rule_id} trigger={reason} "
                    f"recovered={outcome.recovered} 轮次={self._recovery_rounds}/{self.opts.max_recovery_rounds}")

        if outcome.mode == "advise":
            self._recovery_advice = (outcome.advice or "")[:400]
            return {"recovered": False, "packs": [pack]}
        if outcome.recovered:
            self._stall_steps = 0
            self._remember("fact", f"刚由系统层恢复过（{outcome.rule_id}），页面状态可能已重置")
            return {"recovered": True, "packs": [pack]}
        if self._recovery_rounds >= self.opts.max_recovery_rounds:
            return {"fatal": "device_unhealthy",
                    "reason": f"系统层恢复 {self._recovery_rounds} 轮仍未成功（{outcome.rule_id}）",
                    "packs": [pack]}
        return {"recovered": False, "packs": [pack]}

    def _count_decision(self) -> None:
        self._decision_used += 1
        self._wait_rounds = 0

    def _in_create_flow(self) -> bool:
        return self._nested_publish and not self._published

    def _remember(self, kind: str, text: str, *, replace_kind: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        if replace_kind:
            self._memory = [m for m in self._memory if m[0] != replace_kind]
        if any(m[1] == text for m in self._memory):
            return
        self._memory.append((kind, text[:240]))
        sticky = [m for m in self._memory if m[0] == "published"]
        rest = [m for m in self._memory if m[0] != "published"]
        self._memory = sticky + rest[-10:]

    def _note_published(self, payload: Optional[dict[str, Any]] = None, fallback: str = "") -> None:
        bits: list[str] = []
        if isinstance(payload, dict):
            for key in ("title", "when", "note"):
                val = str(payload.get(key) or "").strip()
                if not val:
                    continue
                bits.append(val if key == "note" else f"{key}={val}")
        if not bits and fallback:
            bits.append(fallback[:180])
        if not bits:
            return
        self._published = True
        self._remember("published", "；".join(bits), replace_kind="published")
        SLog.i(TAG, f"[{self.run_id}] published fingerprint: {bits[:3]!r}")

    def _ingest_decision_memory(self, decision, cap: str) -> None:
        for item in decision.remember or []:
            self._remember("fact", item)
            if self._nested_publish and not self._published:
                if _PUBLISHED_RE.search(item) or re.search(r"刚发布|标题\s*[=：:]", item):
                    self._note_published({"note": item})
        if decision.published:
            self._note_published(decision.published, fallback=decision.thought)
        elif _PUBLISHED_RE.search(decision.thought or ""):
            self._note_published({"note": decision.thought[:200]})
        if cap in _MUTATE_CAPS and decision.thought:
            self._remember("before", f"操作前：{decision.thought[:180]}", replace_kind="before")
        if decision.checkpoint_ids:
            self._mark_checkpoints(decision.checkpoint_ids)

    def _mark_checkpoints(self, ids: list[str]) -> None:
        idset = {str(i).strip() for i in ids if str(i).strip()}
        if not idset:
            return
        for cp in self.goal.checkpoints:
            if cp.id in idset or cp.description in idset:
                cp.done = True

    def _mark_next_process_checkpoint(self) -> None:
        for cp in self.goal.checkpoints:
            if not cp.done and getattr(cp, "kind", "terminal") == "process":
                cp.done = True
                return

    def _next_undone_checkpoint(self):
        for cp in self.goal.checkpoints:
            if not cp.done:
                return cp
        return None

    def _skip_nav_verify_checkpoints(self) -> list[str]:
        """入口已点过：不要用「选中态 / 切没切页」挡住后面的操作。"""
        skipped: list[str] = []
        for cp in self.goal.checkpoints:
            if cp.done:
                continue
            if _NAV_VERIFY_RE.search(cp.description or ""):
                cp.done = True
                skipped.append(cp.id)
        if skipped:
            SLog.i(TAG, f"[{self.run_id}] 跳过导航检查点 {skipped}（不检测页面切换）")
        return skipped

    def _assert_context_block(self) -> str:
        parts: list[str] = []
        mem = self._memory_block()
        if mem:
            parts.append("==== 短期记忆（当前截图是现在；下列是之前记下的事实）====\n" + mem)
        process_done = [
            cp for cp in self.goal.checkpoints
            if getattr(cp, "kind", "terminal") == "process" and cp.done
        ]
        if process_done:
            lines = "\n".join(f"- {cp.id}: {cp.description}" for cp in process_done)
            parts.append("过程检查点已在中途验证通过：\n" + lines)
        parts.append(
            "终态不要因为当前屏看不到加载/占位/生成中而判失败。"
            "相对变化（数量+1、样式切换）用记忆中的之前对比当前图；"
            "不要因为当前图上看不到变化前而判失败。"
            "要找刚发布的内容时，用记忆中的标题/时间/文案对照当前图。"
        )
        return "\n".join(parts)

    def _session_block_text(self) -> str:
        return (self._session_note or "").strip()

    _SESSION_NAV_CAPS = {
        "tap", "click", "double_tap", "long_press", "swipe", "scroll",
        "back", "press_back", "input_text",
    }
    _SESSION_SKIP_CAPS = {
        "close_app", "launch_app", "open_app", "skip_restart", "inspect_session",
        "pick_account", "capture_screen", "wait_ms", "wait", "noop",
    }

    def _has_session_probe_nav(self) -> bool:
        """开场重启/发号不算；真正点过页面之后才有资格看登录态。"""
        for s in self.steps:
            cap = str(s.capability_id or "").lower()
            if not cap or cap in self._SESSION_SKIP_CAPS or cap.startswith("recovery_"):
                continue
            if cap in self._SESSION_NAV_CAPS or cap.startswith("tap") or cap.startswith("click"):
                return True
        return False

    def _maybe_inspect_session(self, screen, *, knowledge: list[dict[str, Any]] | None = None) -> None:
        """首页通常看不出登录态。等点过页面后再观察一次；看不清不要转人工。"""
        if self._session_inspected:
            return
        if not self._has_session_probe_nav():
            return
        if screen is None or not screen.has_image():
            return
        required = (self.case_preconditions or "").strip() or (self.goal.goal or "")
        knowledge_hint = self._knowledge_hint(knowledge or [])
        row = planner.inspect_session(
            required_session=required,
            knowledge_hint=knowledge_hint,
            accounts_brief=str(getattr(self.ctx, "accounts_brief", "") or ""),
            image_base64=screen.image_base64,
            image_mime=screen.image_mime,
            provider_id=self.provider_id,
            timeout_sec=self.opts.step_timeout_sec,
        )
        note = (
            f"session={row.get('session')} identity={row.get('identity')} "
            f"next={row.get('next')} probe={row.get('probe')} "
            f"seen={row.get('seen') or '—'}；{row.get('reason') or ''}"
        )
        self._session_note = note.strip()
        self._session_inspected = True
        SLog.i(TAG, f"[{self.run_id}] inspect session {self._session_note[:180]!r}")
        thumb = agent_stream.make_thumb(screen.image_base64)
        shot_hash = _screen_hash(screen.image_base64)
        shot_phash = _screen_phash(screen.image_base64)
        self._record_synthetic(
            len(self.results) + 1, EventStatus.PASS, "inspect_session",
            self._session_note[:180], shot_hash, shot_phash,
            thumb=thumb,
        )
        if self.results and knowledge:
            try:
                self.results[-1] = self.results[-1].model_copy(update={"knowledge": list(knowledge)})
            except Exception:
                pass

    def _maybe_bootstrap_restart(self) -> None:
        """开场看图：由模型决定是否 force-stop + launch。不计入决策预算。"""
        pkg = str(getattr(self.ctx, "target_package", "") or "").strip()
        if not pkg:
            SLog.i(TAG, f"[{self.run_id}] skip restart decide: no target_package")
            return
        screen = capture_screen(
            self.ctx, prefer=self.router.capture_prefer,
            timeout_sec=self.opts.capture_timeout_sec, force_fresh=True,
        )
        if not screen.has_image():
            SLog.w(TAG, f"[{self.run_id}] restart decide skipped, capture failed: {screen.error}")
            return
        restart, thought = planner.decide_restart_app(
            goal=self.goal.goal,
            preconditions=self.case_preconditions,
            target_package=pkg,
            image_base64=screen.image_base64,
            image_mime=screen.image_mime,
            provider_id=self.provider_id,
            timeout_sec=self.opts.step_timeout_sec,
        )
        SLog.i(TAG, f"[{self.run_id}] bootstrap restart={restart} thought={thought[:120]!r}")
        thumb = agent_stream.make_thumb(screen.image_base64)
        shot_hash = _screen_hash(screen.image_base64)
        shot_phash = _screen_phash(screen.image_base64)
        self._last_phash = shot_phash
        if not restart:
            self._record_synthetic(
                len(self.results) + 1, EventStatus.SKIPPED, "skip_restart",
                f"开场不重启：{thought[:180]}", shot_hash, shot_phash,
                thumb=thumb,
            )
            return
        close_idx = len(self.results) + 1
        self._dispatch_bootstrap(
            close_idx, "close_app", {"package": pkg},
            thought=f"开场重启：{thought[:180]}", label="强停目标应用", thumb=thumb,
            shot_hash=shot_hash, phash=shot_phash,
        )
        time.sleep(0.4)
        launch_idx = len(self.results) + 1
        self._dispatch_bootstrap(
            launch_idx, "launch_app", {"package": pkg},
            thought="开场重启后启动目标应用", label="启动目标应用", thumb="", shot_hash="",
        )
        time.sleep(self.opts.restart_settle_sec)

    def _dispatch_bootstrap(
        self, seq: int, cap: str, params: dict, *, thought: str, label: str,
        thumb: str, shot_hash: str, phash: str = "",
    ) -> None:
        event = PlanEvent(
            seq=seq, capability_id=cap, event_kind=cap,
            params=self._normalize_action_params(cap, dict(params)),
            needs_vlm=False, expected_executor="",
            ai_reasoning=thought[:240], label=label,
        )
        result = self.router.dispatch(
            event, run_id=self.run_id, case_id=self.case_id,
            case_brief=self.case_brief, shared=self.shared,
        )
        if thumb and not getattr(result, "thumb", None):
            try:
                result = result.model_copy(update={"thumb": thumb})
            except Exception:
                pass
        self.results.append(result)
        self.steps.append(_Step(
            idx=seq, thought=thought, capability_id=cap, params=dict(params),
            status="continue", result_status=str(result.status.value),
            summary=result.summary or result.error, screen_hash=shot_hash, phash=phash,
        ))
        k = self._match_step_knowledge(extra=f"{cap} {thought}")
        self._emit(
            "result", step=seq, result_status=str(result.status.value),
            summary=result.summary or result.error,
            elapsed_ms=int(getattr(result, "elapsed_ms", 0) or 0),
            capability_id=cap, thumb=thumb,
            knowledge=k,
        )

    def _gate_session_before_loop(self) -> Optional[tuple[str, str, str]]:
        """开业务循环前对齐登录态。不对齐则 fail / untestable，并学习登录流程。"""
        from server.services.runtime import device_provision, session_gate

        keep = bool(getattr(self.ctx, "keep_permission_prompt", False))
        plat = str(getattr(self.ctx, "platform", "") or "android")
        sn = str(getattr(self.ctx, "sn", "") or "")
        try:
            alert = device_provision.accept_post_launch_alerts(
                sn=sn, platform=plat, keep_permission_prompt=keep,
            )
            if alert and not alert.get("skipped"):
                SLog.i(TAG, f"[{self.run_id}] post-launch alert {alert}")
        except Exception as exc:
            SLog.w(TAG, f"[{self.run_id}] post-launch alert failed: {exc}")

        required = session_gate.required_session(self.case_preconditions)
        if required == "any":
            return None

        screen = capture_screen(
            self.ctx, prefer=self.router.capture_prefer,
            timeout_sec=self.opts.capture_timeout_sec, force_fresh=True,
        )
        inspect_row: dict[str, Any] = {}
        if screen.has_image():
            inspect_row = planner.inspect_session(
                required_session=self.case_preconditions or self.goal.goal,
                knowledge_hint="",
                accounts_brief=str(getattr(self.ctx, "accounts_brief", "") or ""),
                image_base64=screen.image_base64,
                image_mime=screen.image_mime,
                provider_id=self.provider_id,
                timeout_sec=self.opts.step_timeout_sec,
            )
            self._session_inspected = True
            self._session_note = (
                f"session={inspect_row.get('session')} identity={inspect_row.get('identity')} "
                f"next={inspect_row.get('next')} seen={inspect_row.get('seen') or '—'}；"
                f"{inspect_row.get('reason') or ''}"
            ).strip()
            self._record_synthetic(
                len(self.results) + 1, EventStatus.PASS, "inspect_session",
                self._session_note[:180],
                _screen_hash(screen.image_base64) if screen.has_image() else "",
                _screen_phash(screen.image_base64) if screen.has_image() else "",
                thumb=agent_stream.make_thumb(screen.image_base64) if screen.has_image() else "",
            )

        fact = session_gate.observe_session(
            sn=sn,
            platform=plat,
            package=str(getattr(self.ctx, "target_package", "") or ""),
            required=required,
            screen_text=str(inspect_row.get("seen") or ""),
            inspect_row=inspect_row,
        )
        self.ctx.session_fact = dict(fact)
        gate = session_gate.evaluate_gate(fact)
        if gate.get("ok"):
            return None

        status = str(gate.get("status") or "fail")
        reason = str(gate.get("reason") or "登录态不满足")
        category = str(gate.get("category") or "goal_unreachable")
        self._record_synthetic(
            len(self.results) + 1,
            EventStatus.FAIL,
            "session_gate",
            reason[:200],
        )
        try:
            from server.services.knowledge_capture_service import capture_login_flow

            capture_login_flow(
                app_id=str(getattr(self.ctx, "app_id", "") or ""),
                task_id=str(getattr(self.ctx, "batch_id", "") or self.run_id),
                case_id=self.case_id,
                case_name=self.case_name,
                required=required,
                observed=str(fact.get("observed") or ""),
                reason=reason,
                screen_text=str(fact.get("screen_text") or fact.get("seen") or ""),
                image_base64=screen.image_base64 if screen.has_image() else "",
                image_mime=screen.image_mime if screen.has_image() else "image/png",
                provider_id=str(self.provider_id or ""),
                untestable=(status == "untestable"),
            )
        except Exception as exc:
            SLog.w(TAG, f"[{self.run_id}] login learn failed: {exc}")
        SLog.w(TAG, f"[{self.run_id}] session gate {status}: {reason[:160]}")
        return status, reason, category

    # ---------- 子过程 ----------

    def _assert_goal(self, screen, step_idx: int) -> tuple[bool, str]:
        t0 = time.time()
        started_at = _now_iso()
        res = planner.assert_visual(
            expectation=self.goal.success_criteria or self.goal.goal,
            image_base64=screen.image_base64, image_mime=screen.image_mime,
            provider_id=self.provider_id, timeout_sec=self.opts.step_timeout_sec,
            context_block=self._assert_context_block(),
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        thumb = agent_stream.make_thumb(screen.image_base64) if screen and screen.has_image() else ""
        status = EventStatus.PASS if res.passed else EventStatus.FAIL
        summary = res.evidence or res.ai_reasoning[:120]
        self.results.append(EventResult(
            seq=step_idx, capability_id="assert_goal", event_kind="assert_visual",
            status=status,
            executor_used="vlm", summary=summary,
            error="" if res.passed else res.ai_reasoning[:240],
            vlm_meta={"confidence": res.confidence}, thumb=thumb,
            elapsed_ms=elapsed_ms,
            started_at=started_at, finished_at=_now_iso(),
        ))
        self._emit(
            "result",
            step=step_idx,
            thumb=thumb,
            result_status=str(status.value),
            summary=summary,
            elapsed_ms=elapsed_ms,
            capability_id="assert_goal",
            knowledge=self._match_step_knowledge(extra=f"assert_goal {summary}"),
        )
        SLog.i(TAG, f"[{self.run_id}] 成功断言 passed={res.passed} conf={res.confidence} "
                    f"elapsed={elapsed_ms}ms {res.ai_reasoning[:80]!r}")
        return res.passed, (res.ai_reasoning or res.evidence or "成功标准未满足")

    def _pool_value_for_field(self, field: str) -> str:
        picked = getattr(self.ctx, "picked_account", None) or {}
        if not isinstance(picked, dict):
            return ""
        if field == "phone":
            phone = re.sub(r"\s+", "", str(picked.get("phone") or ""))
            return phone if re.fullmatch(r"\d{8,13}", phone) else ""
        if field == "sms_code":
            return str(picked.get("sms_code") or "").strip()
        return ""

    def _note_issued_account(self) -> None:
        picked = getattr(self.ctx, "picked_account", None) or {}
        if not isinstance(picked, dict):
            picked = {}
        phone = re.sub(r"\s+", "", str(picked.get("phone") or ""))
        sms = str(picked.get("sms_code") or "").strip()
        env = str(picked.get("env") or "").strip() or "-"
        if phone:
            self._memory.append(
                ("fact", f"号池已申请 {env} 环境手机号 {phone}")
            )
        if sms:
            self._memory.append(
                ("fact", f"该账号固定验证码 {sms}，验证码页直接填写，不要问人")
            )
        if phone:
            summary = f"已申请 {env} {phone}"
            if sms:
                summary += f" 固定验证码 {sms}"
            status = EventStatus.PASS
        elif str(getattr(self.ctx, "accounts_brief", "") or "").strip():
            summary = (self.ctx.accounts_brief or "").split("\n")[0][:180]
            status = EventStatus.SKIPPED
        else:
            return
        self._record_synthetic(len(self.results) + 1, status, "pick_account", summary)

    def _ask_human(self, decision, step_idx: int, shot_hash: str, shot_phash: str = "") -> str:
        norm = self._normalize_hitl(decision)
        if norm is None:
            reason = (
                "需要人工时只能提供系统可填入的信息（手机号/验证码/文本），"
                "不能改为让人在设备上勾选、登录或操作"
            )
            self._record_synthetic(step_idx, EventStatus.FAIL, "give_up", reason, shot_hash, shot_phash)
            return "give_up"
        cap, params = norm
        field = str(params.get("field") or "").strip().lower()
        pooled = self._pool_value_for_field(field)
        if pooled:
            self.shared["hitl_last_answer"] = {
                "request_id": "",
                "kind": "input_text",
                "answer": pooled,
                "source": "account_pool",
                "capability_id": cap,
                "event_seq": step_idx,
            }
            label = "手机号" if field == "phone" else ("验证码" if field == "sms_code" else "文本")
            summary = f"号池已提供{label}，跳过人工输入"
            self._record_synthetic(
                step_idx, EventStatus.PASS, "pick_account", summary, shot_hash, shot_phash,
            )
            SLog.i(TAG, f"[{self.run_id}] skip HITL {field} from account pool")
            return "answered"
        event = PlanEvent(
            seq=step_idx, capability_id=cap, event_kind=cap, params=params,
            needs_vlm=False, expected_executor="hitl",
            ai_reasoning=decision.thought[:240] or "(agent ask_human)",
            label=params.get("question") or "请求人工提供信息",
        )
        result = self.router.dispatch(
            event, run_id=self.run_id, case_id=self.case_id,
            case_brief=self.case_brief, shared=self.shared,
        )
        self.results.append(result)
        self._push_step(step_idx, decision, result_status=str(result.status.value),
                        summary=result.summary or result.error, screen_hash=shot_hash,
                        phash=shot_phash)
        if result.status in (EventStatus.BLOCKED,):
            return "blocked"
        return "answered"

    def _case_allows_account_reset(self) -> bool:
        blob = " ".join([
            self.goal.goal or "",
            self.goal.success_criteria or "",
            self.case_preconditions or "",
            self.case_brief or "",
        ])
        return bool(re.search(r"退出登录|登出|切换账号|清除数据|清缓存", blob))

    def _env_manufacture_reason(self, decision, cap: str) -> str:
        """禁止用登出/清数据/删帖去凑另一种空态。"""
        if self._case_allows_account_reset():
            return ""
        thought = f"{decision.thought or ''} {decision.expected_after or ''}"
        cap = (cap or "").lower()
        if cap == "clear_app_cache" or _CLEAR_ENV_RE.search(thought):
            return "禁止清除数据/缓存来凑前置环境；缺对应账号环境请结束本条"
        if _LOGOUT_RE.search(thought):
            return "禁止退出登录/切换账号；信息流空态与当前主态登录无关，缺空 feed 账号请结束本条"
        if _DELETE_POSTS_RE.search(thought) and _EMPTY_FEED_RE.search(thought):
            return "禁止删帖制造空态；缺空账号环境请结束本条"
        if (
            _PERSONAL_EMPTY_RE.search(thought)
            and re.search(r"社区|信息流|feed", thought, re.I)
            and re.search(r"空态|无内容|为空", thought)
        ):
            return "个人作品/我的发布为 0 不能推出社区信息流为空，禁止把两套空态连着验"
        return ""

    def _infer_hitl_field(self, text: str) -> str:
        blob = text or ""
        if re.search(r"验证码|短信码|sms", blob, re.I):
            return "sms_code"
        if re.search(r"手机号|电话", blob):
            return "phone"
        return "text"

    def _normalize_hitl(self, decision) -> Optional[tuple[str, dict]]:
        """HITL 只采集可填入界面的信息；让人操作设备则改写或拒绝。"""
        cap = decision.action.capability_id if decision.action else ""
        params = dict(decision.action.params or {}) if decision.action else {}
        thought = decision.thought or ""
        question = str(params.get("question") or thought or "")
        blob = f"{question} {thought}"
        asks_device_op = bool(_DEVICE_OP_RE.search(blob))
        loginish = bool(re.search(r"登录|验证码|手机号|短信", blob))

        if asks_device_op and not loginish:
            return None
        if cap not in _HUMAN_CAPS:
            cap = "human_input_text" if loginish or asks_device_op else "human_confirm"
        if cap == "human_acknowledge" and (asks_device_op or loginish):
            cap = "human_input_text"
        if cap == "human_confirm" and (asks_device_op or re.search(r"协助.*登录|去登录", blob)):
            cap = "human_input_text"
        if cap == "human_input_text":
            field = str(params.get("field") or "").strip().lower()
            if field not in {"phone", "sms_code", "text"}:
                field = self._infer_hitl_field(blob)
            if asks_device_op and field == "text":
                field = "sms_code" if re.search(r"验证码|已登录", blob) else "phone"
            params["field"] = field
            if (not params.get("question") or _DEVICE_OP_RE.search(str(params.get("question") or ""))
                    or re.search(r"已登录", str(params.get("question") or ""))):
                params["question"] = _HITL_FIELD_Q[field]
            return cap, params
        if cap == "human_confirm":
            params.setdefault("question", question or "请确认：当前环境是否已满足本条前置？")
            return cap, params
        params.setdefault("question", question)
        return cap, params

    def _push_step(self, idx: int, decision, *, result_status: str, summary: str,
                   screen_hash: str, phash: str = ""):
        self.steps.append(_Step(
            idx=idx, thought=decision.thought,
            capability_id=decision.action.capability_id if decision.action else "",
            params=dict(decision.action.params or {}) if decision.action else {},
            status=decision.status, result_status=result_status,
            summary=summary or "", screen_hash=screen_hash, phash=phash,
        ))

    def _normalize_action_params(self, cap: str, params: dict) -> dict:
        """纠正应用相关动作的包名：不信任模型给的 package，强制/兜底为目标应用包名。

        修复"启动应用时启动了别的 app"：模型看不到/记不住目标包名，容易照示例或看图标
        猜一个包。测试对象就是 target_package，故 launch 类一律覆盖，其它类缺失时兜底。
        """
        tgt = str(getattr(self.ctx, "target_package", "") or "").strip()
        if not tgt:
            return params
        cap = (cap or "").lower()
        if cap in ("launch_app", "open_app", "start_app"):
            if params.get("package") != tgt:
                SLog.i(TAG, f"[{self.run_id}] 覆盖启动包名 {params.get('package')!r} → 目标 {tgt!r}")
                params["package"] = tgt
        elif cap in ("close_app", "kill_app", "clear_app_cache") and not params.get("package"):
            params["package"] = tgt
        return params

    def _record_synthetic(self, idx: int, status: EventStatus, cap: str, summary: str,
                          screen_hash: str = "", phash: str = "",
                          recovery: dict | None = None, thumb: str = ""):
        extra: dict[str, Any] = {}
        if recovery:
            extra["vlm_meta"] = {"recovery": recovery}
        if thumb:
            extra["thumb"] = thumb
        k = self._match_step_knowledge(extra=f"{cap} {summary}")
        if k:
            extra["knowledge"] = list(k)
        self.results.append(EventResult(
            seq=idx, capability_id=cap, event_kind=cap, status=status,
            executor_used="agent", summary=summary[:200],
            error="" if status in (EventStatus.PASS, EventStatus.SKIPPED) else summary[:200],
            started_at=_now_iso(), finished_at=_now_iso(),
            **extra,
        ))
        self.steps.append(_Step(idx=idx, capability_id=cap, status=str(status.value),
                                summary=summary, screen_hash=screen_hash, phash=phash))
        # 合成步也要进直播：以前只落盘，执行中时间线从第一次 decide 才出现
        self._emit(
            "result",
            step=idx,
            result_status=str(status.value),
            summary=summary,
            capability_id=cap,
            thumb=thumb or "",
            knowledge=k,
        )

    def _task_cancelled(self) -> bool:
        try:
            from server.services.regression.case_runner import is_task_cancelled

            return is_task_cancelled(self.run_id)
        except Exception:
            return False

    _COORD_KEYS = ("x", "y", "from_x", "from_y", "to_x", "to_y")

    def _action_key(self, step: _Step) -> tuple:
        """动作的非坐标部分（能力 + 其余参数），坐标单独按容差比较。"""
        params = {k: v for k, v in (step.params or {}).items() if k not in self._COORD_KEYS}
        return (step.capability_id, tuple(sorted((k, str(v)) for k, v in params.items())))

    def _coords_close(self, a: _Step, b: _Step) -> bool:
        """两步落点是否算「同一个目标」。

        用**距离容差**而不是网格量化：量化会被分桶边界劈开
        （实测 VIEW-007 的 455 与 456 在 24px 网格里恰好跨桶，直接失效）。
        注意 params 里的坐标已经是绝对像素（归一化在 planner 里就换算完了）。
        """
        tol = max(0, int(self.opts.coord_tolerance_px))
        pa, pb = a.params or {}, b.params or {}
        for k in self._COORD_KEYS:
            if k not in pa and k not in pb:
                continue
            try:
                va, vb = int(pa.get(k, 0)), int(pb.get(k, 0))
            except (TypeError, ValueError):
                if pa.get(k) != pb.get(k):
                    return False
                continue
            if abs(va - vb) > tol:
                return False
        return True

    def _last_mutate_step(self) -> Optional[_Step]:
        return next(
            (
                s for s in reversed(self.steps)
                if (s.capability_id or "") not in _OSCILLATION_IGNORE_CAPS
            ),
            None,
        )

    def _repeats_last_mutate(self, cap: str, params: dict, *, phash: str = "") -> bool:
        """是否还在点上一次那个入口。不看画面有没有切（phash 忽略）。"""
        last = self._last_mutate_step()
        if not last or last.capability_id != cap:
            return False
        pending = _Step(idx=0, capability_id=cap, params=dict(params or {}))
        return self._coords_close(pending, last)

    def _is_oscillating(self) -> bool:
        """连续 N 步同一交互动作。画面是否切换不作为是否继续后续操作的依据。

        只看会改变界面的交互（点/滑/输入等）。wait / assert / skip_restart
        不计入窗口：轮播文案、加载进度条在感知哈希上几乎同屏，连续等待会被误杀。
        """
        w = self.opts.oscillation_window
        candidates = [
            s for s in self.steps
            if (s.capability_id or "") not in _OSCILLATION_IGNORE_CAPS
        ]
        if len(candidates) < w:
            return False
        tail = candidates[-w:]
        base = tail[0]
        if not base.capability_id:
            return False
        base_key = self._action_key(base)
        for s in tail:
            if self._action_key(s) != base_key or not self._coords_close(base, s):
                return False
        # 屏幕相似度：任一步 phash 未知（截图失败等）→ 不判定，避免误杀
        for s in tail:
            d = _phash_distance(base.phash, s.phash)
            if d < 0 or d > self.opts.phash_max_distance:
                return False
        return True

    def _build_report(self, overall, started_at, started_ts, blocked_reason, decline_reason, failure_category="") -> RunReport:
        counts = {EventStatus.PASS: 0, EventStatus.FAIL: 0, EventStatus.SKIPPED: 0,
                  EventStatus.BLOCKED: 0, EventStatus.DECLINED: 0}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        report = RunReport(
            run_id=self.run_id, case_id=self.case_id, sn=self.ctx.sn or "",
            overall_status=overall,  # type: ignore[arg-type]
            total_events=len(self.results),
            passed=counts[EventStatus.PASS], failed=counts[EventStatus.FAIL],
            skipped=counts[EventStatus.SKIPPED], blocked=counts[EventStatus.BLOCKED],
            declined=counts[EventStatus.DECLINED],
            replan_count=0, events=self.results,
            elapsed_ms=int((time.time() - started_ts) * 1000),
            started_at=started_at, finished_at=_now_iso(),
            decline_reason=decline_reason, blocked_reason=blocked_reason,
        )
        # 统一失败分类（extra="allow"）：success|goal_unreachable|execution_error|budget_exhausted|needs_human|device_unhealthy
        report.failure_category = failure_category or ("success" if overall == "pass" else "")
        # 环境干预可见化：这次跑得干净不干净，是几个数字而不是埋在 wait 里
        report.env_interventions = self._recovery_rounds
        report.recovery_hits = list(self._recovery_hits)
        report.failure_label = _CATEGORY_LABEL.get(report.failure_category, "")
        fact = getattr(self.ctx, "session_fact", None) or {}
        if fact:
            report.session_fact = dict(fact)
        if overall == "untestable":
            report.session_gate = "untestable"
        elif fact:
            report.session_gate = str(fact.get("observed") or "")
        SLog.i(TAG, f"[{self.run_id}] <<< agent case={self.case_id} status={overall} "
                    f"category={report.failure_category} "
                    f"steps={len(self.steps)} ({report.passed}P/{report.failed}F/{report.blocked}B "
                    f"in {report.elapsed_ms}ms)")
        self._emit("done", overall=overall, summary=(blocked_reason or decline_reason),
                   failure_category=report.failure_category, failure_label=report.failure_label)
        return report


def run_agent_case(
    case_spec: CaseSpec,
    *,
    run_context: RunContext,
    router: CapabilityRouter,
    provider_id: Optional[str] = None,
    run_id: str = "",
    options: Optional[AgentOptions] = None,
) -> RunReport:
    """端到端跑一条 case（agent 模式）：extract_goal → AgentExecutor.run。"""
    opts = options or AgentOptions()
    case_steps = count_case_steps(case_spec)
    opts = replace(opts, max_steps=compute_decision_budget(case_spec, opts))
    nested = case_needs_nested_publish(case_spec)
    SLog.i(TAG, f"[{run_id}] decision budget={opts.max_steps} "
                f"(case_steps={case_steps} × {opts.steps_per_case_step}, "
                f"wait 不计入, nested_publish={nested}, cap {opts.min_steps}-{opts.max_steps_cap})")

    goal = planner.extract_goal(case_spec, run_context=run_context, provider_id=provider_id)
    if not nested:
        nested = case_needs_nested_publish(case_spec, extra=f"{goal.goal}\n{goal.success_criteria}")
    SLog.i(TAG, f"[{run_id}] goal extracted: {goal.goal!r} cps={[c.description for c in goal.checkpoints]}")

    ex = AgentExecutor(
        goal=goal, run_context=run_context, router=router,
        run_id=run_id, case_id=case_spec.case_id, case_brief=goal.goal,
        provider_id=provider_id, options=opts,
        case_preconditions=case_spec.preconditions,
        case_name=case_spec.name or "",
        case_steps_text=case_steps_text(case_spec),
        nested_publish=nested,
    )
    report = ex.run()

    # 成功轨迹仍落盘，暂不注入 decide（上次路径占 token，且尚未作为产品能力使用）
    if report.overall_status == "pass":
        from server.services.regression import agent_memory

        device_sig = getattr(run_context, "device_signature", "") or ""
        traj = [
            {"capability_id": s.capability_id, "params": s.params, "thought": (s.thought or "")[:80]}
            for s in ex.steps if s.capability_id and s.capability_id not in (
                "give_up", "noop", "capture_screen", "skip_restart", "inspect_session",
                "pick_account",
            )
        ]
        agent_memory.save_trajectory(case_spec.case_id, device_sig, traj)

    # 落 trace（best-effort，与 plan 模式共用 case_memory；agent 暂不自动 promote baseline）
    try:
        from server.services.regression import case_memory
        from server.services.ai.regression.schemas import PlanResult

        synthetic_plan = PlanResult(
            mode="plan", case_id=case_spec.case_id,
            ai_reasoning=f"agent-mode goal={goal.goal!r}", events=[],
            goal=goal.goal,
            checkpoints=[
                {"id": c.id, "description": c.description, "kind": getattr(c, "kind", "terminal")}
                for c in (goal.checkpoints or [])
            ],
            success_criteria=getattr(goal, "success_criteria", "") or "",
        )
        case_memory.record_run_finished(
            report=report, plan=synthetic_plan, run_context=run_context,
            case_id=case_spec.case_id, auto_bless_on_pass=False, blessed_by="agent",
        )
    except Exception as exc:  # pragma: no cover
        SLog.w(TAG, f"[{run_id}] agent record_run_finished failed: {exc}")

    return report
