# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AgentExecutor：目标导向的闭环执行引擎（D1–D6 改造，仅 adb 通道）。

替代旧的「整体 plan → 逐条跑 → 文本盲 replan」两段式。核心循环：

    observe(每步截图) → decide_next_action(看图直接出坐标) → router.dispatch → re-observe

- D1 用例=目标+检查点   D2 决策 VLM 直接出坐标(不走 locate VLM)   D3 每步看图
- D4 成功交给 VLM 断言   D5 允许 ask_human(走 human_* 能力+现有 HitlExecutor)
- 收尾：墙钟上限（单用例 20 分钟）+ 按步骤编号校验 + 震荡检测
- 步骤指针：做完步骤 n 再验同号预期，禁止跳步；校验不走操作 prompt，只按质检库看图或直接无法验证

底座全复用：CapabilityRouter / executors / 通道 / screen / case_memory。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
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

# 加载轮询（另有 max_wait_rounds）
_WAIT_CAPS = {"wait_ms", "wait_screen_ready"}
# 等待 / 断言 / 开场跳过 不是「动作没落地」。cr-00c044bb6529 五条用例都在
# 连续 wait_ms（轮播引导语、加载进度条）上被误判卡死。这些能力另有
# max_wait_rounds 等上限，不进震荡窗口。
_OSCILLATION_IGNORE_CAPS = {
    "wait_ms", "wait_screen_ready", "assert_visual",
    "skip_restart", "inspect_session", "pick_account", "lease_account",
    "get_otp", "get_phone", "release_account", "exec_script", "capture_screen",
    "skip_repeat_tap", "assert_skip", "session_align", "session_gate", "case_scene",
    "inspect_env", "env_align",
}
_MUTATE_CAPS = {
    "tap_element", "multi_tap", "swipe_element_to_element", "swipe_direction",
    "input_text", "press_key",
}
# 同一入口再点：不检测页面有没有切过去，直接做后续操作。
_PROCEED_AFTER_ENTRY = (
    "【入口已点过】同一位置已经点过。不要再点这里。"
    "本步操作视为做完，进入【本步预期】校验：只看当前屏是什么，禁止点击/关闭/返回来让预期成立。"
)
# 导航选中态是本应用看图事件，不再按关键词跳过。
_NESTED_PUBLISH_RE = re.compile(
    r"再发|发一条新|发布一条新|新帖后|发布新帖|再发布",
)
_OBSERVE_HEAD_RE = re.compile(r"^(查看|检查|观察|确认是否|确认一下|看一下|看看)")
_DO_VERB_RE = re.compile(r"点击|连点|点「|点选|输入|填写|滑动|上滑|下滑|打开应用|启动应用|长按|拖|按返回")
_CHECK_ONLY_CAPS = {"wait_ms", "wait_screen_ready", "assert_visual", "capture_screen"}
_CHECK_SETTLE_SEC = 0.8
_LOADING_HINT_RE = re.compile(r"加载|转圈|转场|占位|生成中|尚未完成")
_MAKE_ABSENT_RE = re.compile(
    r"关闭|关掉|点叉|叉号|隐藏|让它消失|使其不出现|凑成|凑出|让预期成立|为了让检查点",
)
_ABSENT_EXPECT_RE = re.compile(r"不出现|看不到|不可见|没有出现|未出现")
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
SESSION_MENU_IDS = {
    "tap_element", "multi_tap", "long_press_element", "input_text",
    "swipe_direction", "swipe_element_to_element",
    "wait_ms", "wait_screen_ready", "press_key", "launch_app",
}

# 开场/旁路步骤不进 decide 历史：模型看的是当前截图，这些只会挤掉真正做过的动作
_HISTORY_NOISE_CAPS = {
    "skip_restart", "inspect_session", "pick_account", "lease_account",
    "get_otp", "get_phone", "release_account",
    "capture_screen", "noop",
    "inspect_env", "env_align",
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
    if cap == "multi_tap":
        x, y = params.get("x"), params.get("y")
        n = params.get("count") or 6
        if x is not None and y is not None:
            return f"{cap} @{x},{y} ×{n}"
        return f"{cap} ×{n}"
    if cap == "swipe_element_to_element":
        return (
            f"{cap} @{params.get('from_x')},{params.get('from_y')}"
            f"→{params.get('to_x')},{params.get('to_y')}"
        )
    if cap == "swipe_direction":
        return f"{cap} {params.get('direction') or ''}".strip()
    if cap == "input_text":
        field = str(params.get("field") or "").strip().lower()
        if field == "sms_code":
            return f"{cap} field=sms_code"
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
    "budget_exhausted": "执行超时",
    "needs_human": "需人工介入",
    "device_unhealthy": "设备/系统异常",
    "expect_fail": "校验不通过",
    "expect_unverifiable": "无法验证",
    "step_unexecutable": "测试步骤无法执行",
    "prep_insufficient": "执行期-前置准备不足",
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
    max_case_wall_sec: int = 20 * 60     # 单用例墙钟上限（不再用决策步数卡死）
    max_wait_rounds: int = 15            # 连续 wait 上限，防止无限等
    max_create_steps: int = 40           # 嵌套创作/发布子流程上限（发帖成功前不占主循环）
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
    """用例步骤数：优先飞书编号，其次 steps 条数。"""
    n_list = len(case_spec.steps or [])
    raw = ""
    row = case_spec.raw_row or {}
    if isinstance(row, dict):
        raw = str(row.get("steps_raw") or "")
    if not raw and case_spec.steps:
        raw = "\n".join(s.instruction for s in case_spec.steps if s.instruction)
    n_raw = _count_numbered_in_text(raw) if raw else 0
    return max(n_list, n_raw, 1)


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


def is_observe_only_step(instruction: str) -> bool:
    """「查看/检查」且没有点击等动作 → 本步没有操作，直接验预期。"""
    t = str(instruction or "").strip()
    if not t:
        return True
    if _DO_VERB_RE.search(t):
        return False
    return bool(_OBSERVE_HEAD_RE.search(t))


@dataclass
class SeqNode:
    n: int
    instruction: str
    expected: str
    cp_id: str = ""
    observe_only: bool = False


def build_seq_nodes(case_spec: CaseSpec, goal: Optional[CaseGoal] = None) -> list[SeqNode]:
    """按用例步骤编号建指针：步骤 n 做完才验同号预期。"""
    rows: list[tuple[int, str, str]] = []
    for step in case_spec.steps or []:
        n = int(getattr(step, "index", 0) or 0)
        inst = str(getattr(step, "instruction", "") or "").strip()
        exp = str(getattr(step, "expected", "") or "").strip()
        if not n and not inst and not exp:
            continue
        rows.append((n or (len(rows) + 1), inst, exp))
    if not rows:
        raw = ""
        row = case_spec.raw_row or {}
        if isinstance(row, dict):
            raw = str(row.get("steps_raw") or "")
        if not raw:
            raw = case_steps_text(case_spec, max_chars=8000)
        if raw:
            try:
                from server.services.shared.semantic.case_text_semantic_service import (
                    parse_numbered_items_rules,
                )
                for it in parse_numbered_items_rules(raw) or []:
                    t = str(it.get("text") or "").strip()
                    n = int(it.get("num") or 0) or (len(rows) + 1)
                    if t:
                        rows.append((n, t, ""))
            except Exception:
                rows = [(1, raw.strip(), "")]
    if not rows:
        return []
    nodes: list[SeqNode] = []
    for n, inst, exp in rows:
        nodes.append(SeqNode(
            n=n,
            instruction=inst,
            expected=exp,
            cp_id=f"cp{n}" if exp else "",
            observe_only=is_observe_only_step(inst),
        ))
    return nodes


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
        case_expected: str = "",
        nested_publish: bool = False,
        knowledge_hits: list[dict[str, Any]] | None = None,
        seq_nodes: Optional[list[SeqNode]] = None,
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
        self.case_expected = case_expected or ""
        self._nested_publish = bool(nested_publish)
        self.knowledge_hits = knowledge_hits or []
        self.shared: dict[str, Any] = {}
        self._started_ts = 0.0
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
        self._session_mode = False
        self._session_kind = ""
        self._prep_done = False
        self._session_goal = ""
        self._session_success = ""
        self._session_knowledge_cache: Optional[str] = None
        self._briefing_cache: dict[str, Any] = {}
        self._last_briefing = None
        self._seq_nodes: list[SeqNode] = list(seq_nodes or [])
        self._seq_i = 0
        self._seq_phase = "do"
        self._check_waits = 0
        self._expect_codes: dict[int, str] = {}
        self._seq_halted = False
        if self._seq_nodes:
            self._sync_seq_phase()

    # ---------- prompt 片段 ----------

    def _checkpoints_block(self) -> str:
        if self._seq_enabled:
            return self._seq_prompt_block()
        if not self.goal.checkpoints:
            return "（无显式检查点，按目标自行判断进度）"
        return "\n".join(
            f"[{'x' if cp.done else ' '}] {cp.id}({ '过程' if getattr(cp, 'kind', 'terminal') == 'process' else '终态' }): {cp.description}"
            for cp in self.goal.checkpoints
        )

    @property
    def _seq_enabled(self) -> bool:
        return bool(getattr(self, "_seq_nodes", None))

    def _seq_current(self) -> Optional[SeqNode]:
        nodes = getattr(self, "_seq_nodes", None) or []
        i = int(getattr(self, "_seq_i", 0) or 0)
        if 0 <= i < len(nodes):
            return nodes[i]
        return None

    def _all_steps_uncheckable(self) -> bool:
        return bool(self._seq_nodes) and all(not (n.expected or "").strip() for n in self._seq_nodes)

    def _sync_seq_phase(self) -> None:
        cur = self._seq_current()
        if not cur:
            self._seq_phase = "done"
            return
        if cur.observe_only:
            if cur.expected:
                self._seq_phase = "check"
            else:
                self._seq_advance()
            return
        if not cur.instruction and cur.expected:
            self._seq_phase = "check"
            return
        self._seq_phase = "do"
        if not cur.instruction and not cur.expected:
            self._seq_advance()

    def _seq_advance(self) -> bool:
        self._seq_i += 1
        self._check_waits = 0
        self._false_done = 0
        self._assert_feedback = ""
        if self._seq_i >= len(self._seq_nodes):
            self._seq_phase = "done"
            return False
        self._sync_seq_phase()
        if self._seq_phase == "do" and self._seq_current() and not self._seq_current().instruction:
            return self._seq_advance()
        return True

    def _seq_prompt_block(self) -> str:
        lines = [
            "【执行纪律：严格按步骤编号。禁止跳到后面的步骤，禁止提前验后面的预期。】",
        ]
        for i, node in enumerate(self._seq_nodes):
            if i < self._seq_i:
                mark = "x"
                tag = "已完成"
            elif i == self._seq_i:
                mark = ">"
                tag = "操作中" if self._seq_phase == "do" else "校验中"
            else:
                mark = " "
                tag = "未到，禁止执行"
            bit = f"[{mark}] 步骤 {node.n} {tag}：{node.instruction or '（无操作）'}"
            if not node.expected:
                if self._all_steps_uncheckable():
                    bit += " ｜ 本步无预期（无法校验）"
                else:
                    bit += " ｜ 本步无预期（做完即过）"
            elif i < self._seq_i:
                bit += " ｜ 已交系统校验"
            elif self._seq_phase == "check" and i == self._seq_i:
                bit += f" ｜ 预期：{node.expected}"
            else:
                bit += " ｜ 做完后由系统校验"
            lines.append(bit)
        cur = self._seq_current()
        if not cur:
            lines.append("全部步骤已完成。不要再操作设备。")
            return "\n".join(lines)
        if self._seq_phase == "do":
            lines.append(f"【当前只做步骤 {cur.n}】{cur.instruction}")
            lines.append(
                "做完本步操作后 status=done，表示本步操作结束（不是整案结束）。"
                "禁止去做后面步骤。不要自己校验，也不要为了后面的字去改界面。"
            )
            if cur.expected:
                lines.append("本步预期由系统单独校验。现在不要看预期、不要验、不要为它去点。")
        else:
            lines.append(f"【当前只验步骤 {cur.n}】{cur.expected or '（无预期，无法执行校验）'}")
            lines.append("校验由系统直接做，不要操作设备。")
        return "\n".join(lines)

    def _seq_decide_goal(self) -> str:
        cur = self._seq_current()
        if not cur:
            return "完成本步操作"
        return (cur.instruction or "").strip() or "完成本步操作"

    def _seq_decide_success(self) -> str:
        cur = self._seq_current()
        if not cur:
            return "全部步骤已完成"
        return (
            f"完成步骤 {cur.n} 的操作：{cur.instruction}。"
            "不要自己校验预期，不要用整案成功标准，也不要为了后面的字去改界面。"
        )

    def _outcome_manufacture_reason(self, decision, cap: str) -> str:
        """禁止为了让预期成立而改界面（例如关掉悬浮球再验「不出现」）。"""
        cap = (cap or "").lower()
        thought = f"{getattr(decision, 'thought', '')} {getattr(decision, 'expected_after', '')}"
        cur = self._seq_current()
        exp = (cur.expected if cur else "") or ""
        if self._seq_enabled:
            exp = " ".join(n.expected for n in self._seq_nodes[self._seq_i:] if n.expected)
        if self._seq_phase == "check" and cap and cap not in _CHECK_ONLY_CAPS:
            return "校验阶段禁止操作设备来凑预期；按当前屏判定"
        if cap not in _MUTATE_CAPS and cap != "press_key":
            return ""
        if _ABSENT_EXPECT_RE.search(exp) and _MAKE_ABSENT_RE.search(thought):
            return "禁止关闭或隐藏目标来凑「不出现」；屏幕上有什么就验什么"
        if _MAKE_ABSENT_RE.search(thought) and re.search(r"预期|检查点|成功标准", thought):
            return "禁止为了让检查点成立而改界面；按当前屏验证"
        return ""

    def _force_current_assert(self, decision, *, note: str = "") -> None:
        cur = self._seq_current()
        exp = (cur.expected if cur else "") or ""
        decision.status = "continue"
        decision.action = AgentAction(capability_id="assert_visual", params={"expectation": exp})
        if cur and cur.cp_id:
            decision.checkpoint_ids = [cur.cp_id]
        thought = (decision.thought or "").strip()
        suffix = note or "〔校验：只看当前屏，不改界面〕"
        if suffix not in thought:
            decision.thought = f"{thought} {suffix}".strip()

    def _constrain_seq_decision(self, decision, cap: str):
        """卡住当前步骤/阶段：不能提前验后面的点，校验不能改界面。"""
        if not self._seq_enabled:
            return decision, cap
        cur = self._seq_current()
        if not cur:
            decision.status = "done"
            decision.action = None
            return decision, ""
        if cur.cp_id:
            decision.checkpoint_ids = [cur.cp_id]
        else:
            decision.checkpoint_ids = []

        if self._seq_phase == "check":
            if not cur.expected:
                decision.status = "done"
                decision.action = None
                return decision, ""
            if decision.status in ("done", "give_up"):
                self._force_current_assert(decision)
                return decision, "assert_visual"
            if cap not in _CHECK_ONLY_CAPS:
                self._force_current_assert(decision)
                return decision, "assert_visual"
            if cap == "assert_visual" and decision.action:
                params = dict(decision.action.params or {})
                params["expectation"] = cur.expected
                decision.action.params = params
            return decision, cap

        if cap == "assert_visual":
            exp = str((decision.action.params or {}).get("expectation") or "")
            if cur.expected and (cur.expected in exp or exp in cur.expected or not exp):
                self._seq_phase = "check"
                self._force_current_assert(decision)
                return decision, "assert_visual"
            self._record_synthetic(
                len(self.results) + 1, EventStatus.SKIPPED, "skip_out_of_order",
                f"禁止提前验后面的预期，先做完步骤 {cur.n}",
            )
            decision.status = "continue"
            decision.action = None
            return decision, ""
        return decision, cap

    def _seq_on_check_pass(self) -> str:
        """check 通过后前进。返回 pass|continue。"""
        if self._seq_halted:
            return "fail"
        cur = self._seq_current()
        if cur and cur.cp_id:
            self._mark_checkpoints([cur.cp_id])
        if not self._seq_advance():
            return "pass"
        return "continue"

    def _seq_on_check_fail(self, reason: str, *, loading: bool) -> Optional[str]:
        """失败：加载中可再等；否则产品未达。返回 continue 表示继续转圈等待。"""
        if loading and self._check_waits < 3:
            self._check_waits += 1
            self._assert_feedback = reason
            self._oscillation_advice = "当前仍在加载，wait_ms 后再按同一预期校验，不要点关闭。"
            return "continue"
        self._seq_halted = True
        return None

    def _stamp_remaining_unobserved(self, n: int, claims, *, from_i: int) -> None:
        """红了就停：本步后面没看到的句子记未观察，不当失败。"""
        for c in list(claims)[from_i:]:
            if getattr(c, "gap", False):
                self._stamp_expect(n, c.code)
            else:
                self._stamp_expect(n, "EXPECT.SKIPPED.step_not_done")

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
            src = str(ans.get("source") or "resource")
            extras.append(f"[资源网关已填 来源 {src}]")
        if self._assert_feedback and not self._seq_enabled:
            extras.append(f"[校验未通过] {_clip_hist(self._assert_feedback, 80)}")
        left = self._wall_left_label()
        if self._in_create_flow():
            extras.append(
                f"[时限] 创作/发布进行中"
                f"（{self._create_used}/{self.opts.max_create_steps}）；剩余 {left}"
            )
        else:
            extras.append(f"[时限] 剩余 {left}")
        body = "\n".join(lines)
        if len(body) > _HISTORY_BLOCK_MAX_CHARS:
            body = body[-_HISTORY_BLOCK_MAX_CHARS:]
            cut = body.find("\n")
            if cut > 0:
                body = body[cut + 1 :]
        return "\n".join(x for x in (body, *extras) if x)

    def _wall_left_sec(self) -> int:
        started = self._started_ts or time.time()
        return max(0, int(self.opts.max_case_wall_sec - (time.time() - started)))

    def _wall_left_label(self) -> str:
        m, s = divmod(self._wall_left_sec(), 60)
        return f"{m}分{s}秒"

    def _wall_exceeded(self) -> bool:
        if self._started_ts <= 0:
            return False
        return (time.time() - self._started_ts) >= max(1, int(self.opts.max_case_wall_sec))

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
        cur = self._seq_current()
        steps_text = self.case_steps_text
        if cur:
            open_cps = [x for x in (cur.instruction, cur.expected) if x]
            steps_text = cur.instruction or steps_text
        return build_case_intent_for_knowledge(
            case_name=self.case_name,
            goal=self.goal.goal or self.case_brief,
            steps_text=steps_text,
            preconditions=self.case_preconditions,
            success_criteria=(cur.expected if cur else "") or self.goal.success_criteria or "",
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
            bind = item.get("bind") if isinstance(item.get("bind"), dict) else {}
            has_bind = bool(str(bind.get("slot") or "").strip() and str(bind.get("value") or "").strip())
            used = item.get("used") is not False and bool(body or has_bind)
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
        from server.services.knowledge_situation import route_knowledge
        from server.services.system_settings_service import dedupe_knowledge_hits

        query = self._knowledge_query(extra=extra, dump=dump)
        try:
            hits = route_knowledge(
                query,
                app_id=str(getattr(self.ctx, "app_id", "") or ""),
                scene=self._knowledge_scene(),
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
        packet = self._compile_briefing(dump=dump, extra=extra)
        ranked = self._knowledge_rows_from_hits(getattr(packet, "knowledge", None) or [])
        self.knowledge_hits = ranked
        return ranked

    def _knowledge_hint(self, rows: list[dict[str, Any]]) -> str:
        return build_knowledge_index_text(rows)

    def _compose_knowledge_hint(self, rows: list[dict[str, Any]], *, body: str = "") -> str:
        parts: list[str] = []
        packet = self._last_briefing or self._compile_briefing()
        if getattr(packet, "text", ""):
            parts.append(packet.text)
        elif rows:
            parts.append(self._knowledge_hint(rows))
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
        hier = ""
        try:
            from server.services.runtime.playwright_hub import get_hub, is_web_slot

            if is_web_slot(getattr(self.ctx, "sn", ""), getattr(self.ctx, "platform", "")):
                hier = get_hub().a11y_text(str(getattr(self.ctx, "sn", "") or ""))
        except Exception:
            hier = ""
        if self._session_mode:
            goal = self._session_goal
            success = self._session_success
            if getattr(self, "_session_kind", "") == "env":
                checkpoints = (
                    "（环境对齐）只确认/切换本应用当前环境。"
                    "按说明书或知识里的切换路径操作。不要登录，不要做业务步骤。"
                    "若不在目标应用内，先打开目标应用。"
                )
            else:
                checkpoints = (
                    "（备会话阶段）只做本应用的登录、退出或切到已租账号。"
                    "每个应用入口完全不同：看截图，按本应用说明书/知识逐步操作，"
                    "不要套用通用「我的→设置→退出」或通用底栏。"
                    "当屏在问手机号或一次性口令时 input_text 并带 field=phone 或 field=sms_code，"
                    "text 可写占位，值由资源网关填入。禁止 ask_human 要号或码，禁止自己编造。"
                    "若不在目标应用内，先打开目标应用。不要开始业务步骤。"
                )
        else:
            goal = self._seq_decide_goal() if self._seq_enabled else self.goal.goal
            success = self._seq_decide_success() if self._seq_enabled else self.goal.success_criteria
            checkpoints = self._checkpoints_block()
        return planner.decide_next_action(
            goal=goal,
            success_criteria=success,
            checkpoints_block=checkpoints,
            run_context=self.ctx,
            history_block=self._history_block(),
            width=screen.width, height=screen.height,
            image_base64=screen.image_base64, image_mime=screen.image_mime,
            hierarchy_text=hier,
            knowledge_hint=knowledge_hint,
            memory_block=self._memory_block(),
            session_block=self._session_block_text(),
            provider_id=self.provider_id, timeout_sec=self.opts.step_timeout_sec,
            menu_ids=SESSION_MENU_IDS if self._session_mode else None,
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
        """屏幕文案只给知识检索当 hint，不当登录/业务结论。"""
        try:
            from server.services.runtime.playwright_hub import get_hub, is_web_slot
            from server.services.regression.hierarchy import UiDump

            serial = str(getattr(self.ctx, "sn", "") or "")
            if is_web_slot(serial, getattr(self.ctx, "platform", "")):
                text = get_hub().a11y_text(serial)
                return UiDump(ok=bool(text), source="playwright", raw_text=text)
            from server.services.regression import hierarchy as H
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
        cap = capability_id
        if not cap and decision is not None and getattr(decision, "action", None):
            cap = str(decision.action.capability_id or "")
        data["lane"] = self._lane(cap)
        agent_stream.emit_agent_event(data)

    # ---------- 主循环 ----------

    def run(self) -> RunReport:
        started_ts = time.time()
        self._started_ts = started_ts
        started_at = _now_iso()
        overall = "fail"
        blocked_reason = ""
        decline_reason = ""
        failure_category = ""   # success | goal_unreachable | execution_error | budget_exhausted | needs_human
        wall_min = max(1, int(self.opts.max_case_wall_sec // 60))
        SLog.i(TAG, f"[{self.run_id}] >>> agent case={self.case_id} goal={self.goal.goal!r} "
                    f"checkpoints={len(self.goal.checkpoints)} wall_limit={wall_min}min")
        self._emit("start")
        self._seed_task_memory()
        self._note_issued_account()
        self._maybe_bootstrap_restart()
        gated = None
        try:
            gated = self._gate_env_before_loop()
        except Exception as exc:
            SLog.w(TAG, f"[{self.run_id}] env gate crashed: {exc}")
            gated = ("fail", f"环境对齐异常: {exc}", "prep_insufficient")
        if gated:
            overall, decline_reason, failure_category = gated
            return self._build_report(
                overall, started_at, started_ts, blocked_reason, decline_reason, failure_category,
            )
        gated = None
        try:
            gated = self._gate_session_before_loop()
        except Exception as exc:
            SLog.w(TAG, f"[{self.run_id}] session gate crashed: {exc}")
            gated = None
        if gated:
            overall, decline_reason, failure_category = gated
            return self._build_report(
                overall, started_at, started_ts, blocked_reason, decline_reason, failure_category,
            )
        self._prep_done = True

        capture_fails = 0
        while True:
            if self._wall_exceeded():
                decline_reason = (
                    f"单用例执行超过 {wall_min} 分钟仍未完成目标"
                    f"（已决策 {self._decision_used}）"
                )
                overall = "partial"
                failure_category = "budget_exhausted"
                break
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
            step_idx = len(self.results) + 1
            if self._seq_enabled and self._seq_phase == "check":
                if self._seq_halted:
                    overall = "fail"
                    failure_category = failure_category or "expect_fail"
                    decline_reason = decline_reason or "校验不通过"
                    break
                cur = self._seq_current()
                if cur and not cur.expected:
                    outcome, reason = self._run_seq_check(screen, step_idx)
                    self._count_decision()
                    overall, decline_reason, failure_category = self._apply_check_outcome(outcome, reason)
                    if overall:
                        break
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
                self._emit(
                    "think",
                    step=step_idx,
                    thumb=thumb,
                    summary="正在校验…",
                    knowledge=shown_knowledge,
                )
                outcome, reason = self._run_seq_check(screen, step_idx)
                self._count_decision()
                overall, decline_reason, failure_category = self._apply_check_outcome(outcome, reason)
                if overall:
                    break
                time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                continue
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
            decision, cap = self._constrain_seq_decision(decision, cap)
            if self._seq_enabled:
                made = self._outcome_manufacture_reason(decision, cap)
                if made:
                    self._record_synthetic(step_idx, EventStatus.FAIL, "give_up", made, shot_hash, shot_phash)
                    self._count_decision()
                    overall = "fail"
                    decline_reason = made
                    failure_category = "expect_fail"
                    break
            if self._seq_enabled and self._seq_phase == "check":
                # 校验必须用操作之后的新图，不要拿决策时那张旧屏去判。
                self._count_decision()
                time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                continue
            self._ingest_decision_memory(decision, cap)
            SLog.i(TAG, f"[{self.run_id}] step{step_idx} status={decision.status} "
                        f"act={cap or '-'} decision={self._decision_used} "
                        f"wall_left={self._wall_left_label()} "
                        f"thought={decision.thought[:80]!r}")
            self._emit(
                "step",
                step=step_idx,
                thumb=thumb,
                decision=decision,
                knowledge=shown_knowledge,
            )

            # ---- done：有步骤指针时 = 本步操作结束，进入同号校验；无指针才用整案成功标准 ----
            seq_check_after_action = False
            if decision.status == "done":
                if self._seq_enabled:
                    cur = self._seq_current()
                    if not cur:
                        overall = "pass"
                        failure_category = "success"
                        self._count_decision()
                        break
                    pending_mutate = bool(cap in _MUTATE_CAPS and decision.action)
                    if pending_mutate:
                        decision.status = "continue"
                        seq_check_after_action = True
                    else:
                        self._seq_phase = "check"
                        self._count_decision()
                        time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                        continue
                else:
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
                if self._seq_enabled:
                    self._count_decision()
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
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
                SLog.w(TAG, f"[{self.run_id}] 同一入口再点，跳过点击")
                self._record_synthetic(
                    step_idx, EventStatus.SKIPPED, "skip_repeat_tap",
                    "入口已点过，跳过这次点击",
                    shot_hash, shot_phash, thumb=thumb,
                )
                self._count_decision()
                self._static_repeat = 1
                self._oscillation_advice = _PROCEED_AFTER_ENTRY
                self._remember("fact", "入口已点过，不再重复点击")
                if self._seq_enabled:
                    cur = self._seq_current()
                    if cur and cur.expected:
                        self._seq_phase = "check"
                        time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                        continue
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
                elif self._followup_after_repeat:
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
                else:
                    self._followup_after_repeat = True
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
                    decision.thought = f"入口已点过，改为验证：{nxt.description}"
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
            self.results.append(self._adopt(result, cap))
            self._push_step(step_idx, decision, result_status=str(result.status.value),
                            summary=result.summary or result.error, screen_hash=shot_hash,
                            phash=shot_phash)
            self._emit(
                "result",
                step=step_idx,
                result_status=str(result.status.value),
                summary=result.summary or result.error,
                elapsed_ms=int(getattr(result, "elapsed_ms", 0) or 0),
                capability_id=cap,
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

            if seq_check_after_action and result.status == EventStatus.PASS:
                self._seq_phase = "check"
                time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                continue

            if cap == "assert_visual" and result.status == EventStatus.PASS:
                if result.summary:
                    self._remember("observed", result.summary[:180])
                if self._seq_enabled:
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
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
            elif cap == "assert_visual" and self._seq_enabled:
                reason = result.error or result.summary or "本步预期未在当前屏成立"
                loading = bool(_LOADING_HINT_RE.search(reason))
                if self._seq_on_check_fail(reason, loading=loading) == "continue":
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
                cur = self._seq_current()
                if cur:
                    from server.services.regression.expect_catalog import classify_expect_text
                    row = classify_expect_text(cur.expected)
                    done = len([p for p in str(self._expect_codes.get(cur.n) or "").split("|") if p.strip()])
                    if not done:
                        head = next((c for c in row.claims if not c.gap), None)
                        self._stamp_expect(cur.n, f"EXPECT.FAIL.{getattr(head, 'kind', None) or 'unknown'}")
                        done = 1
                    self._stamp_remaining_unobserved(cur.n, row.claims, from_i=done)
                overall = "fail"
                failure_category = "expect_fail"
                decline_reason = reason[:240]
                break

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
        try:
            from server.services.runtime.playwright_hub import is_web_slot

            if is_web_slot(getattr(self.ctx, "sn", ""), getattr(self.ctx, "platform", "")):
                return ""
        except Exception:
            pass
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
        if decision.checkpoint_ids and not self._seq_enabled:
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
        """选中态是本应用看图事件，不再按关键词跳过。"""
        return []

    def _assert_context_block(self) -> str:
        parts: list[str] = []
        try:
            packet = self._compile_briefing(extra_scene={
                "lane": "expect",
                "need": "judge_selected",
                "facet": "chrome",
                "screen_role": "chrome_nav",
            })
            if getattr(packet, "text", ""):
                parts.append(packet.text)
        except Exception as exc:
            SLog.d(TAG, f"[{self.run_id}] expect briefing skipped: {exc}")
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

    def _maybe_bootstrap_restart(self) -> None:
        """开场看图：由模型决定是否 force-stop + launch。不计入决策预算。"""
        pkg = str(getattr(self.ctx, "target_package", "") or "").strip()
        if not pkg:
            SLog.i(TAG, f"[{self.run_id}] skip restart decide: no target_package")
            return
        try:
            from server.services.runtime.playwright_hub import is_web_slot

            if is_web_slot(getattr(self.ctx, "sn", ""), getattr(self.ctx, "platform", "")):
                self._record_synthetic(
                    len(self.results) + 1, EventStatus.SKIPPED, "skip_restart",
                    "网页槽已打开目标网址，不再强停重启",
                )
                return
        except Exception:
            pass
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
        self.results.append(self._adopt(result, cap))
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

    def _picked_phone(self) -> str:
        picked = getattr(self.ctx, "picked_account", None) or {}
        if not isinstance(picked, dict):
            return ""
        return re.sub(r"\s+", "", str(picked.get("phone") or ""))

    def _task_session_same_account(self) -> bool:
        sess = getattr(self.ctx, "task_session", None) or {}
        if not sess.get("logged_in"):
            return False
        phone = self._picked_phone()
        if phone and sess.get("phone") and str(sess.get("phone")) != phone:
            return False
        return True

    def _knowledge_surface(self) -> str:
        try:
            from server.services.runtime.playwright_hub import is_web_slot

            if is_web_slot(getattr(self.ctx, "sn", ""), getattr(self.ctx, "platform", "")):
                return "web"
        except Exception:
            pass
        return "app"

    def _account_env(self) -> str:
        picked = getattr(self.ctx, "picked_account", None) or {}
        if not isinstance(picked, dict):
            return ""
        return str(picked.get("env") or "").strip()

    def _knowledge_scene(self, extra: Optional[dict] = None) -> dict[str, str]:
        scene: dict[str, str] = {
            "surface": self._knowledge_surface(),
        }
        if self._session_mode:
            scene["lane"] = "prep"
            scene["need"] = "howto"
        elif self._seq_enabled and str(getattr(self, "_seq_phase", "") or "") == "check":
            scene["lane"] = "expect"
            scene["need"] = "judge_selected"
            scene["facet"] = "chrome"
            scene["screen_role"] = "chrome_nav"
        else:
            scene["lane"] = self._lane("")
            if scene["lane"] == "expect":
                scene["need"] = "judge_selected"
                scene["facet"] = "chrome"
                scene["screen_role"] = "chrome_nav"
        if extra:
            scene.update({k: str(v) for k, v in extra.items() if v})
        return scene

    def _compile_briefing(self, *, dump=None, extra: str = "", extra_scene: Optional[dict] = None, synthesize: bool = False):
        from server.services.knowledge_briefing import briefing_cache_key, compile_briefing

        scene = self._knowledge_scene(extra_scene)
        query = self._knowledge_query(extra=extra, dump=dump)
        intent = self._case_intent_for_knowledge()
        key = briefing_cache_key(
            str(getattr(self.ctx, "app_id", "") or ""),
            scene,
            query=query[:400],
            extra=intent[:200],
            app_version=str(getattr(self.ctx, "app_version", "") or ""),
            env_profile=str(getattr(self.ctx, "env_profile", "") or ""),
        )
        hit = self._briefing_cache.get(key)
        if hit is not None:
            self._last_briefing = hit
            return hit
        packet = compile_briefing(
            str(getattr(self.ctx, "app_id", "") or ""),
            scene,
            query=query,
            case_intent=intent,
            playbook=getattr(self.ctx, "playbook", None),
            synthesize=synthesize,
            app_version=str(getattr(self.ctx, "app_version", "") or ""),
            env_profile=str(getattr(self.ctx, "env_profile", "") or ""),
            env_label=str(getattr(self.ctx, "env_label", "") or ""),
        )
        self._briefing_cache[key] = packet
        self._last_briefing = packet
        return packet

    def _value_for_field(self, field: str) -> str:
        from server.services.resources.gateway import resolve_secret

        return str(resolve_secret(self.ctx, field).value or "").strip()

    def _session_knowledge_text(self) -> str:
        """会话阶段注入编译简报（路径/绑定/壳层），不要求模型先点名。"""
        if self._session_knowledge_cache is not None:
            return self._session_knowledge_cache
        try:
            packet = self._compile_briefing(extra_scene={
                "lane": "prep",
                "need": "howto",
            })
            text = str(getattr(packet, "text", "") or "").strip()
        except Exception as exc:
            SLog.w(TAG, f"[{self.run_id}] session briefing failed: {exc}")
            text = ""
        if not text:
            try:
                from server.services.ai.playbook_service import session_howto_block
                text = session_howto_block(getattr(self.ctx, "playbook", None)) or ""
            except Exception:
                text = ""
        self._session_knowledge_cache = text.strip()
        return self._session_knowledge_cache

    def _inspect_session_row(self, screen, *, knowledge_hint: str = "") -> dict[str, Any]:
        if screen is None or not screen.has_image():
            return {
                "session": "unknown", "identity": "unknown", "seen": "",
                "probe": False, "next": "keep", "reason": "无截图", "ok": False,
            }
        knowledge_hint = (knowledge_hint or "").strip() or self._session_knowledge_text()
        return planner.inspect_session(
            required_session=self.case_preconditions or self.goal.goal,
            knowledge_hint=knowledge_hint,
            accounts_brief=str(getattr(self.ctx, "accounts_brief", "") or ""),
            image_base64=screen.image_base64,
            image_mime=screen.image_mime,
            provider_id=self.provider_id,
            timeout_sec=self.opts.step_timeout_sec,
        )

    def _record_inspect_row(self, screen, row: dict[str, Any]) -> None:
        note = (
            f"session={row.get('session')} identity={row.get('identity')} "
            f"next={row.get('next')} probe={row.get('probe')} "
            f"seen={row.get('seen') or '—'}；{row.get('reason') or ''}"
        )
        self._session_note = note.strip()
        self._session_inspected = True
        ok = bool(row.get("ok"))
        SLog.i(TAG, f"[{self.run_id}] inspect session ok={ok} {self._session_note[:180]!r}")
        status = EventStatus.PASS if ok else EventStatus.SKIPPED
        summary = (
            self._session_note[:180]
            if ok
            else f"看图会话观察失败（已重试），不作为已确认登录态：{(row.get('reason') or '')[:100]}"
        )
        self._record_synthetic(
            len(self.results) + 1, status, "inspect_session",
            summary,
            _screen_hash(screen.image_base64) if screen and screen.has_image() else "",
            _screen_phash(screen.image_base64) if screen and screen.has_image() else "",
            thumb=agent_stream.make_thumb(screen.image_base64) if screen and screen.has_image() else "",
        )

    def _commit_task_session(self, fact: dict[str, Any]) -> None:
        observed = str(fact.get("observed") or "unknown")
        self.ctx.task_session = {
            "phone": self._picked_phone(),
            "logged_in": observed == "logged_in",
            "observed": observed,
            "identity": str(fact.get("identity") or "unknown"),
        }
        self.ctx.session_fact = dict(fact)
        self.ctx.session_dirty = False

    def _fill_pool_input_params(self, params: dict, decision) -> dict:
        text = str(params.get("text") or params.get("value") or "").strip()
        field = str(params.get("field") or "").strip().lower()
        blob = f"{getattr(decision, 'thought', '')} {params}"
        if field not in {"sms_code", "phone"}:
            if re.search(r"验证码|sms", blob, re.I):
                field = "sms_code"
            elif re.search(r"手机|phone", blob, re.I):
                field = "phone"
            else:
                field = ""
        if field:
            pooled = self._value_for_field(field)
        else:
            pooled = self._value_for_field("phone") or self._value_for_field("sms_code")
        placeholder = (not text) or text in {"验证码", "手机号", "请输入"}
        if pooled and (placeholder or field or re.search(r"验证码|sms|手机|phone", blob, re.I)):
            params["text"] = pooled
            params["value"] = pooled
            if field:
                params["field"] = field
        return params

    def _run_session_actions(self, intent: str, *, max_steps: int = 12) -> bool:
        fill_bit = (
            "当屏在问手机号时 input_text，params.field=phone；"
            "当屏在问一次性口令时 input_text，params.field=sms_code。"
            "text 可写占位，值由资源网关填入。禁止问人，禁止自己编造。"
        )
        custom = "按【本应用自己的】登录/退出方式操作（说明书与知识），不要套用其它 App 的界面结构。"
        if intent == "login":
            self._session_goal = (
                f"{custom}把当前应用登录到已租账号。{fill_bit}"
                "若不在目标应用内，先打开目标应用。不要开始业务操作。"
            )
            self._session_success = "已登录到该账号，落在稳定主界面（不是登录页）"
        elif intent == "switch":
            self._session_goal = (
                f"{custom}当前已登录但不是目标号。先按本应用方式退出，再登录已租账号。"
                f"{fill_bit}不要做业务。"
            )
            self._session_success = "已切换到已租账号并落在主界面"
        elif intent == "relogin":
            self._session_goal = (
                f"{custom}先退出当前登录（已在登录页则不必退出），再登录已租账号。"
                f"{fill_bit}不要开始业务操作。"
            )
            self._session_success = "已登录到已租账号，落在稳定主界面（不是登录页）"
        elif intent == "env":
            want = str(getattr(self.ctx, "env_label", "") or getattr(self.ctx, "env_profile", "") or "目标环境")
            self._session_kind = "env"
            self._session_goal = (
                f"按本应用自己的方式把客户端切到「{want}」。"
                "只走设置/调试/关于里的环境切换。不要登录，不要注册，不要做业务。"
                "若不在目标应用内，先打开目标应用。"
            )
            self._session_success = f"当前屏能看出已经是 {want}"
        else:
            self._session_kind = "session"
            self._session_goal = (
                f"{custom}退出当前登录，回到未登录/游客。不要注册，不要登录，不要做业务。"
            )
            self._session_success = "当前是登录页或游客态"
        if intent != "env":
            self._session_kind = "session"
            phone = self._picked_phone()
            self._record_synthetic(
                len(self.results) + 1, EventStatus.PASS, "session_align",
                f"本趟任务备会话：{intent} {phone or '—'}",
            )
        hint = self._session_knowledge_text()
        self._session_mode = True
        try:
            for _ in range(max_steps):
                if self._task_cancelled() or self._wall_exceeded():
                    return False
                screen = capture_screen(
                    self.ctx, prefer=self.router.capture_prefer,
                    timeout_sec=self.opts.capture_timeout_sec, force_fresh=True,
                )
                if not screen.has_image():
                    time.sleep(1.0)
                    continue
                thumb = agent_stream.make_thumb(screen.image_base64)
                shot_hash = _screen_hash(screen.image_base64)
                shot_phash = _screen_phash(screen.image_base64)
                step_idx = len(self.results) + 1
                self._emit("think", step=step_idx, thumb=thumb, summary=f"备会话：{intent}")
                decision = self._decide(screen, knowledge_hint=hint)
                cap = decision.action.capability_id if decision.action else ""
                if decision.status == "done":
                    return True
                if decision.status == "give_up":
                    if intent == "env":
                        return False
                    self._record_synthetic(
                        step_idx, EventStatus.FAIL, "session_align",
                        (decision.thought or "会话对齐放弃")[:200],
                        shot_hash, shot_phash, thumb=thumb,
                    )
                    return False
                if decision.status == "ask_human":
                    res = self._ask_human(decision, step_idx, shot_hash, shot_phash)
                    if res == "answered":
                        time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                        continue
                    return False
                if not cap:
                    time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                    continue
                params = self._normalize_action_params(cap, dict(decision.action.params or {}))
                if cap == "input_text":
                    params = self._fill_pool_input_params(params, decision)
                event = PlanEvent(
                    seq=step_idx, capability_id=cap, event_kind=cap,
                    params=params, needs_vlm=False, expected_executor="",
                    ai_reasoning=(decision.thought or "")[:240] or "(session)",
                    label=f"备会话 {intent}",
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
                self.results.append(self._adopt(result, cap))
                self._push_step(
                    step_idx, decision, result_status=str(result.status.value),
                    summary=result.summary or result.error, screen_hash=shot_hash, phash=shot_phash,
                )
                self._emit(
                    "result", step=step_idx,
                    result_status=str(result.status.value),
                    summary=result.summary or result.error,
                    elapsed_ms=int(getattr(result, "elapsed_ms", 0) or 0),
                    capability_id=cap, thumb=thumb,
                )
                time.sleep(self.opts.pause_ms_between_steps / 1000.0)
            return False
        finally:
            self._session_mode = False
            self._session_kind = ""

    def run_env_switch(self, wanted: str = "", label: str = "") -> bool:
        """开跑前把客户端切到本趟环境。不租号、不登录。"""
        if wanted and hasattr(self.ctx, "env_profile") and not str(getattr(self.ctx, "env_profile", "") or ""):
            self.ctx.env_profile = wanted
        if label and hasattr(self.ctx, "env_label"):
            self.ctx.env_label = label
        return self._run_session_actions("env", max_steps=10)

    def _record_env_step(self, cap: str, status: str, summary: str) -> None:
        st = {
            "pass": EventStatus.PASS,
            "skipped": EventStatus.SKIPPED,
            "fail": EventStatus.FAIL,
        }.get(str(status or "").strip().lower(), EventStatus.PASS)
        self._record_synthetic(len(self.results) + 1, st, cap, summary)

    def _gate_env_before_loop(self) -> Optional[tuple[str, str, str]]:
        """开业务循环前对齐客户端环境。unknown（登录页无角标）不是冲突。"""
        from server.services.runtime.env_gate import align_device_env, public_env_snapshot

        prev = dict(getattr(self.ctx, "env_fact", None) or {})
        if prev.get("ok"):
            label = str(prev.get("label") or getattr(self.ctx, "env_label", "") or "")
            bit = "已确认" if prev.get("matched") else "当前屏未看出标识"
            self._record_synthetic(
                len(self.results) + 1, EventStatus.SKIPPED, "env_align",
                f"沿用本趟环境：{label or prev.get('wanted') or '—'}（{bit}）",
            )
            return None
        plat = str(getattr(self.ctx, "platform", "") or "")
        report = align_device_env(
            self.ctx, self.router,
            package=str(getattr(self.ctx, "target_package", "") or ""),
            platform=plat,
            run_id=self.run_id,
            case_id=self.case_id,
            provider_id=self.provider_id or "",
            capture_prefer=tuple(self.router.capture_prefer or ("adb", "remote")),
            recorder=self._record_env_step,
            switch_fn=lambda: self._run_session_actions("env", max_steps=10),
        )
        try:
            from server.services.regression.case_runner import note_run_env_fact

            note_run_env_fact(self.run_id, report, public_env_snapshot(self.ctx))
        except Exception:
            pass
        if report.get("ok"):
            return None
        reason = str(report.get("reason") or "设备当前环境与本趟执行环境不一致")
        return ("fail", reason, "prep_insufficient")

    def _case_scene(self) -> dict:
        from server.services.runtime.session_gate import clamp_case_scene

        existing = getattr(self.ctx, "case_scene", None) or {}
        if existing.get("session_prep"):
            return clamp_case_scene(existing)
        from server.services.ai.regression.planner import classify_case_scene

        scene = classify_case_scene(
            name=self.case_name,
            steps=self.case_steps_text,
            expected=self.case_expected,
            precondition=self.case_preconditions,
            provider_id=self.provider_id,
            run_context=self.ctx,
        )
        self.ctx.case_scene = dict(scene)
        return scene

    def _reuse_task_session_without_inspect(self, required: str, *, reason: str) -> None:
        sess = dict(getattr(self.ctx, "task_session", None) or {})
        observed = str(sess.get("observed") or "")
        if sess.get("logged_in") and observed != "logged_in":
            observed = "logged_in"
        fact = {
            "required": required,
            "observed": observed or "unknown",
            "identity": str(sess.get("identity") or "unknown"),
            "how": "task_memory",
            "reason": reason,
            "inspect_ok": True,
        }
        self.ctx.session_fact = dict(fact)
        self._session_note = f"session={fact['observed']} reused from task_memory"
        self._session_inspected = True
        self._record_synthetic(
            len(self.results) + 1, EventStatus.SKIPPED, "inspect_session",
            "沿用本趟 RunContext 登录态，未重新看图",
        )

    def _commit_prep_session(
        self, *, required: str, observed: str, how: str, reason: str,
    ) -> None:
        fact = {
            "required": required,
            "observed": observed,
            "identity": "unknown",
            "how": how,
            "reason": reason,
            "inspect_ok": True,
            "phone": self._picked_phone(),
        }
        self._commit_task_session(fact)
        self._session_note = f"session={observed} how={how}"
        self._session_inspected = True
        self._record_synthetic(
            len(self.results) + 1, EventStatus.SKIPPED, "inspect_session",
            "前置已对齐登录态并写入 RunContext，跳过观察态",
        )

    def _observe_current_session(self, *, sn: str, plat: str, required: str):
        from server.services.runtime import session_gate

        screen = capture_screen(
            self.ctx, prefer=self.router.capture_prefer,
            timeout_sec=self.opts.capture_timeout_sec, force_fresh=True,
        )
        inspect_row: dict[str, Any] = {}
        if screen.has_image():
            inspect_row = self._inspect_session_row(screen)
            self._record_inspect_row(screen, inspect_row)
        fact = session_gate.observe_session(
            sn=sn,
            platform=plat,
            package=str(getattr(self.ctx, "target_package", "") or ""),
            required=required,
            screen_text=str(inspect_row.get("seen") or ""),
            inspect_row=inspect_row,
        )
        self.ctx.session_fact = dict(fact)
        return screen, inspect_row, fact

    def _fail_session_gate(
        self, required: str, fact: dict[str, Any], screen, *, status: str, reason: str, category: str,
    ) -> tuple[str, str, str]:
        self._record_synthetic(
            len(self.results) + 1, EventStatus.FAIL, "session_gate", reason[:200],
        )
        self._learn_login_flow(
            required, fact, screen, untestable=(status == "untestable"), reason=reason,
        )
        SLog.w(TAG, f"[{self.run_id}] session gate {status}: {reason[:160]}")
        return status, reason, category

    def _gate_session_before_loop(self) -> Optional[tuple[str, str, str]]:
        """开业务循环前对齐登录态。

        业务用例：前置退出并重新登录，写入 RunContext，不再看图观察。
        登录相关用例：不自动登录。
        """
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

        required = session_gate.required_session(self.case_preconditions, scene=self._case_scene())
        prep = self._session_prep_intent()
        scene = self._case_scene()
        self._record_synthetic(
            len(self.results) + 1, EventStatus.PASS, "case_scene",
            (
                f"session_prep={prep} required={required} how={scene.get('how') or '—'}"
                f"；{str(scene.get('reason') or '')[:160]}"
            ),
        )
        dirty = bool(getattr(self.ctx, "session_dirty", False))
        sess = getattr(self.ctx, "task_session", None) or {}
        phone = self._picked_phone()
        SLog.i(
            TAG,
            f"[{self.run_id}] session prep={prep} required={required} "
            f"phone={phone or '—'} dirty={dirty}",
        )

        if prep == "skip":
            self._record_synthetic(
                len(self.results) + 1, EventStatus.SKIPPED, "session_align",
                "登录相关用例，不自动登录或退出",
            )
            if session_gate.can_reuse_task_session(
                required="any", task_session=sess, picked_phone=phone, dirty=dirty,
            ):
                self._reuse_task_session_without_inspect(
                    required, reason="登录相关用例不自动登录，沿用本趟记录，未看图",
                )
                return None
            self._observe_current_session(sn=sn, plat=plat, required=required)
            return None

        reuse_need = "guest" if prep == "logout" else "logged_in"
        if session_gate.can_reuse_task_session(
            required=reuse_need, task_session=sess, picked_phone=phone, dirty=dirty,
        ):
            self._reuse_task_session_without_inspect(
                required, reason="沿用本趟任务已对齐的登录态",
            )
            return None

        if prep == "logout":
            if self._run_session_actions("logout"):
                self._commit_prep_session(
                    required=required, observed="guest", how="prep_logout",
                    reason="前置已退出登录（登录相关用例不自动登录）",
                )
                return None
            self.ctx.session_dirty = True
            self.ctx.task_session = {}
            screen, _, fact = self._observe_current_session(sn=sn, plat=plat, required=required)
            if required != "guest":
                return None
            gate = session_gate.evaluate_gate(fact)
            if gate.get("ok"):
                return None
            return self._fail_session_gate(
                required, fact, screen,
                status=str(gate.get("status") or "fail"),
                reason=str(gate.get("reason") or "需要游客/未登录，前置退出未完成"),
                category=str(gate.get("category") or "goal_unreachable"),
            )

        if not phone:
            reason = "需要已登录，账号管理未租到手机号，无法自动登录"
            self._record_synthetic(len(self.results) + 1, EventStatus.FAIL, "session_gate", reason)
            self._learn_login_flow(required or "logged_in", {}, None, untestable=False, reason=reason)
            return "fail", reason, "goal_unreachable"

        if self._run_session_actions("relogin", max_steps=18):
            self._commit_prep_session(
                required=required or "logged_in", observed="logged_in", how="prep_login",
                reason="前置已退出并重新登录已租账号，未再看图观察",
            )
            return None

        screen, _, fact = self._observe_current_session(
            sn=sn, plat=plat, required=required or "logged_in",
        )
        if fact.get("wechat_untestable"):
            reason = "需要已登录，当前是微信登录页，无法自动化（untestable）"
            return self._fail_session_gate(
                required or "logged_in", fact, screen,
                status="untestable", reason=reason, category="goal_unreachable",
            )
        gate = session_gate.evaluate_gate(fact)
        if gate.get("ok") and str(fact.get("observed") or "") == "logged_in":
            self._commit_task_session(fact)
            return None
        return self._fail_session_gate(
            required or "logged_in", fact, screen,
            status=str(gate.get("status") or "fail"),
            reason=str(gate.get("reason") or "前置退出并重新登录未完成"),
            category=str(gate.get("category") or "goal_unreachable"),
        )

    def _learn_login_flow(self, required: str, fact: dict, screen, *, untestable: bool, reason: str) -> None:
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
                image_base64=screen.image_base64 if screen and screen.has_image() else "",
                image_mime=screen.image_mime if screen and screen.has_image() else "image/png",
                provider_id=str(self.provider_id or ""),
                untestable=untestable,
            )
        except Exception as exc:
            SLog.w(TAG, f"[{self.run_id}] login learn failed: {exc}")

    # ---------- 子过程 ----------

    def _stamp_expect(self, n: int, code: str) -> None:
        if not n:
            return
        bits = [p.strip() for p in str(code or "").split("|") if p.strip()]
        if not bits:
            return
        prev = str(self._expect_codes.get(int(n)) or "")
        if prev:
            have = {p.strip() for p in prev.split("|") if p.strip()}
            bits = [p for p in bits if p not in have]
            if not bits:
                return
            self._expect_codes[int(n)] = prev + "|" + "|".join(bits)
            return
        self._expect_codes[int(n)] = "|".join(bits)

    def _stamp_check_row(self, n: int, row, *, observed_ok=None) -> None:
        claims = list(getattr(row, "claims", None) or [])
        if not claims:
            if observed_ok is True:
                self._stamp_expect(n, f"EXPECT.PASS.{getattr(row, 'kind', '') or 'unknown'}")
            elif observed_ok is False:
                self._stamp_expect(n, f"EXPECT.FAIL.{getattr(row, 'kind', '') or 'unknown'}")
            else:
                self._stamp_expect(n, getattr(row, "code", "") or "EXPECT.UNKNOWN")
            return
        codes = []
        for c in claims:
            if c.gap:
                codes.append(c.code)
            elif observed_ok is True:
                codes.append(f"EXPECT.PASS.{c.kind}")
            elif observed_ok is False:
                codes.append(f"EXPECT.FAIL.{c.kind}")
            else:
                codes.append(c.code)
        self._stamp_expect(n, "|".join(codes))

    def _settle_screen(self, screen, *, kind: str = ""):
        if kind in {"page_nav", "text_present", "text_absent", "node", "meaning"}:
            time.sleep(_CHECK_SETTLE_SEC)
        if not self.router:
            return screen
        fresh = capture_screen(
            self.ctx, prefer=self.router.capture_prefer,
            timeout_sec=self.opts.capture_timeout_sec, force_fresh=True,
        )
        return fresh if fresh and fresh.has_image() else screen

    def _web_page(self):
        try:
            from server.services.runtime.playwright_hub import get_hub, is_web_slot

            if not is_web_slot(getattr(self.ctx, "sn", ""), getattr(self.ctx, "platform", "")):
                return None
            return get_hub().current_page(str(getattr(self.ctx, "sn", "") or ""))
        except Exception as exc:
            SLog.d(TAG, f"[{self.run_id}] playwright check skipped: {exc}")
            return None

    def _observe_claim(self, screen, claim, *, page) -> tuple[str, str]:
        """看一句。返回 pass|fail|wait|skip 与原因。"""
        from server.services.regression.expect_catalog import ExpectClass, gap_summary
        from server.services.regression.playwright_check import check_expect

        if claim.gap:
            mini = ExpectClass(
                kind=claim.kind, code=claim.code, label=claim.label, gap=True,
                prompt_text="", skipped=[claim], claims=[claim],
            )
            self._record_synthetic(
                len(self.results) + 1, EventStatus.SKIPPED, "assert_skip",
                gap_summary(mini) or "无法验证",
                _screen_hash(screen.image_base64) if screen and screen.has_image() else "",
                _screen_phash(screen.image_base64) if screen and screen.has_image() else "",
                thumb=agent_stream.make_thumb(screen.image_base64) if screen and screen.has_image() else "",
            )
            return "skip", gap_summary(mini) or "无法验证"
        mini = ExpectClass(
            kind=claim.kind, code=claim.code, label=claim.label, gap=False,
            prompt_text=claim.text, claims=[claim],
        )
        if page is not None:
            try:
                dom = check_expect(page, mini)
            except Exception as exc:
                SLog.d(TAG, f"[{self.run_id}] playwright claim skipped: {exc}")
                dom = None
            if dom is not None:
                ok, reason = dom
                self._record_synthetic(
                    len(self.results) + 1,
                    EventStatus.PASS if ok else EventStatus.FAIL,
                    "assert_dom",
                    reason,
                    _screen_hash(screen.image_base64) if screen and screen.has_image() else "",
                    _screen_phash(screen.image_base64) if screen and screen.has_image() else "",
                    thumb=agent_stream.make_thumb(screen.image_base64) if screen and screen.has_image() else "",
                )
                return ("pass" if ok else "fail"), reason
        ok, reason = self._assert_expectation(
            screen, len(self.results) + 1, claim.text, cap_id="assert_visual",
        )
        if ok:
            return "pass", reason
        loading = bool(_LOADING_HINT_RE.search(reason or ""))
        if self._seq_on_check_fail(reason, loading=loading) == "continue":
            return "wait", reason
        return "fail", reason

    def _run_seq_check(self, screen, step_idx: int) -> tuple[str, str]:
        """按句校验当前步骤。一句红了就停，后面句子和步骤都不再验。"""
        from server.services.regression.expect_catalog import classify_expect_text, gap_summary

        if self._seq_halted:
            return "fail", "校验不通过"
        cur = self._seq_current()
        if not cur:
            return "pass", ""
        if not cur.expected:
            self._stamp_expect(cur.n, "EXPECT.SKIPPED.no_expect")
            nxt = self._seq_on_check_pass()
            if nxt == "fail":
                return "fail", "校验不通过"
            if nxt == "pass" and self._all_steps_uncheckable():
                return "unexecutable", "未写预期，无法执行校验"
            return ("pass", "") if nxt == "pass" else ("advance", "")
        row = classify_expect_text(cur.expected)
        claims = list(row.claims or [])
        if not claims:
            self._stamp_check_row(cur.n, row)
            nxt = self._seq_on_check_pass()
            if nxt == "fail":
                return "fail", "校验不通过"
            return ("pass", "") if nxt == "pass" else ("advance", gap_summary(row) or "无法验证")
        page = self._web_page()
        if any(not c.gap for c in claims):
            screen = self._settle_screen(screen, kind=row.kind)
        saw_real = False
        done = len([p for p in str(self._expect_codes.get(cur.n) or "").split("|") if p.strip()])
        for i, claim in enumerate(claims):
            if i < done:
                if not claim.gap:
                    saw_real = True
                continue
            outcome, reason = self._observe_claim(screen, claim, page=page)
            if outcome == "wait":
                return "wait", reason
            if outcome == "skip":
                self._stamp_expect(cur.n, claim.code)
                continue
            saw_real = True
            if outcome == "pass":
                self._stamp_expect(cur.n, f"EXPECT.PASS.{claim.kind}")
                continue
            self._stamp_expect(cur.n, f"EXPECT.FAIL.{claim.kind}")
            self._stamp_remaining_unobserved(cur.n, claims, from_i=i + 1)
            self._seq_halted = True
            return "fail", reason
        if not saw_real:
            nxt = self._seq_on_check_pass()
            if nxt == "fail":
                return "fail", "校验不通过"
            note = gap_summary(row) or "无法验证"
            if nxt == "pass" and self._all_steps_uncheckable():
                return "unexecutable", "未写预期，无法执行校验"
            return ("pass", "") if nxt == "pass" else ("advance", note)
        nxt = self._seq_on_check_pass()
        if nxt == "fail":
            return "fail", "校验不通过"
        return ("pass", "") if nxt == "pass" else ("advance", "")

    def _expect_has_gap(self) -> bool:
        blob = "|".join(str(v or "") for v in self._expect_codes.values())
        return "UNVERIFIABLE" in blob or "UNKNOWN" in blob or "step_not_done" in blob

    def _apply_check_outcome(self, outcome: str, reason: str) -> tuple[str, str, str]:
        """把校验结果变成 (overall, decline, failure_category)。空 overall 表示继续循环。"""
        if outcome == "fail":
            self._seq_halted = True
            return "fail", (reason or "校验不通过")[:240], "expect_fail"
        if outcome == "unexecutable":
            return "fail", (reason or "测试步骤无法执行")[:240], "step_unexecutable"
        if outcome == "pass":
            if self._all_steps_uncheckable():
                return "fail", "未写预期，无法执行校验", "step_unexecutable"
            if self._expect_has_gap():
                return "unverifiable", "无法验证", "expect_unverifiable"
            return "pass", "", "success"
        return "", "", ""

    def _assert_goal(self, screen, step_idx: int) -> tuple[bool, str]:
        return self._assert_expectation(
            screen, step_idx,
            self.goal.success_criteria or self.goal.goal,
            cap_id="assert_goal",
        )

    def _assert_expectation(self, screen, step_idx: int, expectation: str, *, cap_id: str) -> tuple[bool, str]:
        t0 = time.time()
        started_at = _now_iso()
        extra = (
            "只根据当前截图判定。图上有的东西就是有；禁止假设会被关掉或点掉。"
            "不要因为「可以关掉所以算不出现」而判通过。"
        )
        ctx = (self._assert_context_block() + "\n" + extra).strip()
        res = planner.assert_visual(
            expectation=expectation or self.goal.goal,
            image_base64=screen.image_base64, image_mime=screen.image_mime,
            provider_id=self.provider_id, timeout_sec=self.opts.step_timeout_sec,
            context_block=ctx,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        thumb = agent_stream.make_thumb(screen.image_base64) if screen and screen.has_image() else ""
        status = EventStatus.PASS if res.passed else EventStatus.FAIL
        summary = res.evidence or res.ai_reasoning[:120]
        self.results.append(EventResult(
            seq=step_idx, capability_id=cap_id, event_kind="assert_visual",
            lane="expect",
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
            capability_id=cap_id,
            knowledge=self._match_step_knowledge(extra=f"{cap_id} {summary}"),
        )
        SLog.i(TAG, f"[{self.run_id}] 断言 {cap_id} passed={res.passed} conf={res.confidence} "
                    f"elapsed={elapsed_ms}ms {res.ai_reasoning[:80]!r}")
        return res.passed, (res.ai_reasoning or res.evidence or "本步预期未在当前屏成立")

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

    def _seed_task_memory(self) -> None:
        """上条用例记下的事实（发过的消息、刚登录的号）带到本条，不重验产品点。"""
        for item in list(getattr(self.ctx, "task_memory", None) or [])[-8:]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            kind = str(item.get("kind") or "fact").strip() or "fact"
            self._remember(kind, text)

    def _note_issued_account(self) -> None:
        picked = getattr(self.ctx, "picked_account", None) or {}
        if not isinstance(picked, dict):
            picked = {}
        phone = re.sub(r"\s+", "", str(picked.get("phone") or ""))
        env = str(picked.get("env") or "").strip() or "-"
        tail = phone[-4:] if len(phone) >= 4 else phone
        from server.services.resources.gateway import resolve_secret

        otp = resolve_secret(self.ctx, "sms_code")
        if phone:
            self._memory.append(
                ("fact", f"已租 {env} 账号尾号 {tail}，登录页 field=phone 由资源网关填写")
            )
        if otp.value or otp.source == "hitl":
            src = otp.source if otp.value else "hitl"
            self._memory.append(
                ("fact", f"口令来源 {src}，口令页 field=sms_code 由资源网关填写，不要问人、不要写出口令")
            )
        if phone:
            summary = f"已租 {env} 尾号 {tail}"
            if otp.value:
                summary += f" 口令来源 {otp.source}"
            elif otp.source == "hitl":
                summary += " 口令需问人"
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
        from server.services.resources.gateway import resolve_secret

        hit = resolve_secret(self.ctx, field) if field else None
        pooled = str(hit.value or "") if hit else ""
        if pooled:
            source = str(hit.source or "resource")
            self.shared["hitl_last_answer"] = {
                "request_id": "",
                "kind": "input_text",
                "answer": pooled,
                "source": source,
                "capability_id": cap,
                "event_seq": step_idx,
            }
            label = "手机号" if field == "phone" else ("一次性口令" if field == "sms_code" else "文本")
            cap_name = "get_phone" if field == "phone" else ("get_otp" if field == "sms_code" else "pick_account")
            summary = f"资源网关已提供{label}（{source}），跳过人工输入"
            self._record_synthetic(
                step_idx, EventStatus.PASS, cap_name, summary, shot_hash, shot_phash,
            )
            SLog.i(TAG, f"[{self.run_id}] skip HITL {field} from {source}")
            return "answered"
        event = PlanEvent(
            seq=step_idx, capability_id=cap, event_kind=cap, params=params,
            needs_vlm=False, expected_executor="hitl",
            ai_reasoning=decision.thought[:240] or "(agent ask_human)",
            label=params.get("question") or "请求人工提供信息",
        )
        self._emit(
            "step",
            step=step_idx,
            decision=decision,
            capability_id=cap,
            summary=str(params.get("question") or "请求人工提供信息"),
        )
        result = self.router.dispatch(
            event, run_id=self.run_id, case_id=self.case_id,
            case_brief=self.case_brief, shared=self.shared,
        )
        self.results.append(self._adopt(result, cap))
        self._push_step(step_idx, decision, result_status=str(result.status.value),
                        summary=result.summary or result.error, screen_hash=shot_hash,
                        phash=shot_phash)
        self._emit(
            "result",
            step=step_idx,
            result_status=str(result.status.value),
            summary=result.summary or result.error,
            elapsed_ms=int(getattr(result, "elapsed_ms", 0) or 0),
            capability_id=cap,
        )
        if result.status in (EventStatus.BLOCKED,):
            return "blocked"
        return "answered"

    def _case_allows_account_reset(self) -> bool:
        from server.services.runtime.session_gate import scene_allows_account_reset

        return scene_allows_account_reset(self._case_scene())

    def _env_manufacture_reason(self, decision, cap: str) -> str:
        """禁止用登出/清数据/删帖去凑另一种空态。会话对齐阶段允许退出/切号。"""
        if self._session_mode or self._case_allows_account_reset():
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
        elif cap in ("close_app", "kill_app", "clear_app_cache", "get_app_version") and not params.get("package"):
            params["package"] = tgt
        return params

    def _lane(self, cap: str = "") -> str:
        from server.services.packs.exec_classes import lane_for_event

        return lane_for_event(
            cap=cap,
            prep_done=bool(getattr(self, "_prep_done", False)),
            session_mode=bool(getattr(self, "_session_mode", False)),
            seq_phase=str(getattr(self, "_seq_phase", "") or "") if self._seq_enabled else "",
        )

    def _adopt(self, result: EventResult, cap: str = "") -> EventResult:
        lane = self._lane(cap or str(getattr(result, "capability_id", "") or ""))
        try:
            return result.model_copy(update={"lane": lane})
        except Exception:
            try:
                result.lane = lane
            except Exception:
                pass
            return result

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
            seq=idx, capability_id=cap, event_kind=cap, lane=self._lane(cap), status=status,
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
        if overall == "pass" and self._expect_has_gap():
            overall = "unverifiable"
            failure_category = "expect_unverifiable"
            decline_reason = decline_reason or "无法验证"
        if overall == "pass" and self._all_steps_uncheckable():
            overall = "fail"
            failure_category = "step_unexecutable"
            decline_reason = decline_reason or "未写预期，无法执行校验"
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
        report.failure_category = failure_category or (
            "success" if overall == "pass" else ("expect_unverifiable" if overall == "unverifiable" else "")
        )
        # 环境干预可见化：这次跑得干净不干净，是几个数字而不是埋在 wait 里
        report.env_interventions = self._recovery_rounds
        report.recovery_hits = list(self._recovery_hits)
        report.failure_label = _CATEGORY_LABEL.get(report.failure_category, "")
        if self._expect_codes:
            report.expect_outcomes = {str(k): v for k, v in self._expect_codes.items()}
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
    nested = case_needs_nested_publish(case_spec)
    SLog.i(TAG, f"[{run_id}] wall_limit={opts.max_case_wall_sec}s "
                f"(case_steps={case_steps}, nested_publish={nested})")

    scene = planner.classify_case_scene(
        case_spec, provider_id=provider_id, run_context=run_context,
    )
    SLog.i(
        TAG,
        f"[{run_id}] case_scene prep={scene.get('session_prep')} "
        f"required={scene.get('required_session')} how={scene.get('how')}",
    )

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
        case_expected=case_spec.expected or "",
        nested_publish=nested,
        seq_nodes=build_seq_nodes(case_spec, goal),
    )
    report = ex.run()

    carry = []
    for kind, text in getattr(ex, "_memory", []) or []:
        if kind in ("published", "message", "fact") and text:
            carry.append({"kind": kind, "text": text})
    if carry:
        prev = list(getattr(run_context, "task_memory", None) or [])
        run_context.task_memory = (prev + carry)[-20:]
    dirty_caps = {"kill_app", "clear_app_cache"}
    if any(str(getattr(s, "capability_id", "") or "") in dirty_caps for s in ex.steps):
        run_context.session_dirty = True
        run_context.task_session = {}
    from server.services.runtime.session_gate import is_login_related_case

    if is_login_related_case(getattr(run_context, "case_scene", None)):
        # 登录/退出/游客用例可能改掉登录态，下一条必须重新前置，不能沿用。
        run_context.session_dirty = True
        run_context.task_session = {}

    # 成功轨迹仍落盘，暂不注入 decide（上次路径占 token，且尚未作为产品能力使用）
    gap_outcomes = any(
        "UNVERIFIABLE" in str(v) or "UNKNOWN" in str(v)
        for v in (getattr(report, "expect_outcomes", None) or {}).values()
    )
    if report.overall_status == "pass" and not gap_outcomes:
        from server.services.regression import agent_memory

        device_sig = getattr(run_context, "device_signature", "") or ""
        traj = [
            {"capability_id": s.capability_id, "params": s.params, "thought": (s.thought or "")[:80]}
            for s in ex.steps if s.capability_id and s.capability_id not in (
                "give_up", "noop", "capture_screen", "skip_restart", "inspect_session",
                "pick_account", "lease_account", "get_otp", "get_phone",
                "session_align", "session_gate", "case_scene",
                "inspect_env", "env_align",
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
