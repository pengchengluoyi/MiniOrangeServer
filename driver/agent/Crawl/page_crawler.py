# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
应用内跑图：遍历页面，每页采集 5 张有效截图，记录跳转路径，按点击组件命名页面。

规则摘要：
- 同页多图：每张需与已采集图相似度 ∈ [min_similarity, max_similarity]（默认 50%~85%）
- 相似度过高 → 先操作（滑动/点击）再重截；过低 → 认为离开当前页
- 离开当前页时记录导航操作（from → to）
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from script.log import SLog
from script.sleep import mSleep
from driver.agent.Crawl.image_similarity import frame_similarity, is_valid_same_page_shot
from driver.agent.Crawl.action_policy import CrawlActionPolicy
from driver.agent.Crawl.hotspot_discovery import (
    click_targets_for_explore,
    discover_hotspots_from_frames,
    explore_targets_for_same_page,
    navigation_targets_from_hotspots,
    _default_content_band,
)
from driver.agent.Crawl.ui_discovery import (
    ClickTarget,
    discover_clickables_from_hierarchy,
    discover_clickables_ocr,
    page_name_from_label,
)
from driver.agent.Perception.Vision.mImageMatching import ImageVision
from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

TAG = "PageCrawler"

SHOTS_PER_PAGE = 5
DEFAULT_MAX_SIM = 0.85
DEFAULT_MIN_SIM = 0.50
MAX_SHOT_ATTEMPTS = 8
MAX_PAGES = 40
MAX_CLICKS_PER_PAGE = 18
EXPLORE_FEED_DIRECTIONS = ("up", "left", "down")
MAX_LEFT_PAGE_RECOVER = 3
# 交互比例见 CrawlActionPolicy：约 80% 点击 / 20% 滑动；返回键每 50 次手势最多 1 次


@dataclass
class NavRecord:
    from_node_id: str
    to_node_id: str
    action: Dict[str, Any]
    similarity_to_from: float = 0.0


@dataclass
class PageCaptureResult:
    node_id: str
    label: str
    screenshot_paths: List[str] = field(default_factory=list)
    image_files: List[str] = field(default_factory=list)
    natural_size: Optional[Dict] = None


@dataclass
class CrawlReport:
    graph_id: int
    pages: List[PageCaptureResult] = field(default_factory=list)
    navigations: List[NavRecord] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class PageCrawler:
    def __init__(
        self,
        graph_id: int,
        device_sn: str,
        platform: str = "android",
        *,
        package_name: Optional[str] = None,
        max_similarity: float = DEFAULT_MAX_SIM,
        min_similarity: float = DEFAULT_MIN_SIM,
        shots_per_page: int = SHOTS_PER_PAGE,
        max_pages: int = MAX_PAGES,
        output_dir: Optional[str] = None,
    ):
        self.graph_id = graph_id
        self.device_sn = device_sn
        self.platform = platform.lower()
        self.package_name = package_name
        self.max_similarity = max_similarity
        self.min_similarity = min_similarity
        self.shots_per_page = shots_per_page
        self.max_pages = max_pages
        self.output_dir = output_dir
        self.report = CrawlReport(graph_id=graph_id)
        self._engine = None
        self._screen_size: Tuple[int, int] = (1080, 1920)
        self._visited_signatures: List[np.ndarray] = []
        self._known_pages: Dict[str, np.ndarray] = {}
        self._action_index = 0
        self._explored_edges: set = set()
        self._page_hotspots: List[ClickTarget] = []
        self._explore_hotspots: List[ClickTarget] = []
        self._hotspot_shot_key = -1
        self._left_page_recoveries = 0
        self._policy = CrawlActionPolicy()
        self._explore_click_idx = 0

    def _setup_device(self) -> None:
        self._engine, self._screen_size = bootstrap_mobile_engine(
            self.device_sn,
            self.platform,
        )

    def _launch_app(self) -> None:
        if self.package_name:
            self._engine.start_app(self.package_name)
            mSleep(3)

    def _ensure_app_foreground(self) -> None:
        """应用被切到后台时重新拉到前台（不用系统返回键）。"""
        if not self.package_name:
            return
        SLog.i(TAG, f"Bring app to foreground: {self.package_name}")
        self._engine.start_app(self.package_name)
        mSleep(2.5)

    def _capture_gray(self) -> Optional[np.ndarray]:
        img = ImageVision.get_golden_frame(count=2)
        if img is None:
            pil = self._engine.screenshot()
            if pil is None:
                return None
            img = np.array(pil)
            if img.ndim == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        return img

    def _refresh_page_hotspots(self, shots: List[np.ndarray]) -> None:
        w, h = self._screen_size
        self._page_hotspots = discover_hotspots_from_frames(shots, w, h)
        self._explore_hotspots = explore_targets_for_same_page(
            self._page_hotspots, screen_w=w, screen_h=h,
        )

    def _swipe_in_hotspot(self, target: ClickTarget, direction: str) -> None:
        if hasattr(self._engine, "swipe_in_rect"):
            self._engine.swipe_in_rect(target.x, target.y, target.w, target.h, direction)
            return
        cx, cy = target.center
        sw, sh = self._screen_size
        if direction == "up":
            self._engine.swipe_norm(cx / sw, 0.72, cx / sw, 0.38, 0.32)
        elif direction == "down":
            self._engine.swipe_norm(cx / sw, 0.38, cx / sw, 0.72, 0.32)
        elif direction == "left":
            self._engine.swipe_norm(0.78, cy / sh, 0.22, cy / sh, 0.35)
        else:
            self._engine.swipe_norm(0.22, cy / sh, 0.78, cy / sh, 0.35)

    def _after_gesture(self) -> None:
        self._policy.record_gesture()
        self._policy.maybe_flush_back(self._press_back_immediate)

    def _do_feed_swipe(self, direction: str = "up") -> None:
        w, h = self._screen_size
        band = _default_content_band(w, h)
        SLog.i(TAG, f"Explore swipe 20%: {direction}")
        self._swipe_in_hotspot(band, direction)
        mSleep(1.2)
        self._after_gesture()

    def _interact_for_new_shot(self, shots: List[np.ndarray]) -> None:
        """采下一张图前：80% 点热区，20% 滑 Feed。"""
        w, h = self._screen_size
        if shots:
            self._refresh_page_hotspots(shots)
        click_pool = click_targets_for_explore(
            self._page_hotspots, w, h,
        ) if self._page_hotspots else []

        if self._policy.should_swipe():
            direction = EXPLORE_FEED_DIRECTIONS[
                (self._action_index // 1) % len(EXPLORE_FEED_DIRECTIONS)
            ]
            self._action_index += 1
            self._do_feed_swipe(direction)
            return

        if click_pool:
            target = click_pool[self._explore_click_idx % len(click_pool)]
            self._explore_click_idx += 1
            self._click_target(target, tag="explore")
            return

        self._do_feed_swipe("up")

    def collect_page_screenshots(self) -> Tuple[List[np.ndarray], Optional[str]]:
        """
        采集当前页 shots_per_page 张有效截图。
        返回 (images, error_reason)
        """
        shots: List[np.ndarray] = []
        attempts = 0

        while len(shots) < self.shots_per_page and attempts < MAX_SHOT_ATTEMPTS:
            attempts += 1
            if len(shots) > 0:
                self._interact_for_new_shot(shots)

            frame = self._capture_gray()
            if frame is None:
                continue

            if not shots:
                shots.append(frame)
                SLog.i(TAG, f"Page shot 1/{self.shots_per_page}")
                continue

            ok, reason, max_sim = is_valid_same_page_shot(
                frame,
                shots,
                max_similarity=self.max_similarity,
                min_similarity=self.min_similarity,
            )
            if reason == "too_similar":
                SLog.d(TAG, f"Shot rejected too_similar max={max_sim:.2f}, retry")
                continue
            if reason == "left_page":
                self._left_page_recoveries += 1
                SLog.w(
                    TAG,
                    f"Screen changed a lot (max_sim={max_sim:.2f}), recover app "
                    f"({self._left_page_recoveries}/{MAX_LEFT_PAGE_RECOVER})",
                )
                self._ensure_app_foreground()
                if self._left_page_recoveries >= MAX_LEFT_PAGE_RECOVER:
                    if shots:
                        return shots, f"incomplete_{len(shots)}"
                    return shots, "left_page"
                continue

            shots.append(frame)
            self._left_page_recoveries = 0
            SLog.i(TAG, f"Page shot {len(shots)}/{self.shots_per_page} (max_sim={max_sim:.2f})")

        if len(shots) < self.shots_per_page:
            return shots, f"incomplete_{len(shots)}"
        return shots, None

    def _page_signature(self, img: np.ndarray) -> np.ndarray:
        return img

    def _match_known_page(self, img: np.ndarray, threshold: float = 0.72) -> Optional[str]:
        best_id, best_score = None, -1.0
        for node_id, ref in self._known_pages.items():
            score = frame_similarity(img, ref)
            if score > best_score:
                best_score = score
                best_id = node_id
        if best_id and best_score >= threshold:
            return best_id
        return None

    def _is_new_page(self, img: np.ndarray) -> bool:
        for sig in self._visited_signatures:
            if frame_similarity(img, sig) >= 0.72:
                return False
        return True

    def _press_back_immediate(self) -> None:
        SLog.i(TAG, "Execute deferred back key")
        if hasattr(self._engine, "press_key"):
            self._engine.press_key("back")
        elif hasattr(self._engine, "keyevent"):
            self._engine.keyevent("back")
        mSleep(1.0)

    def _request_back(self) -> None:
        """登记返回意图；满 50 次点击/滑动后才真正按返回。"""
        self._policy.schedule_back()
        self._policy.maybe_flush_back(self._press_back_immediate)

    def _needs_back_after_visit(self, via_action: Optional[ClickTarget]) -> bool:
        """底栏 Tab 切换无需按返回（会把 App 切到后台）。"""
        if not via_action:
            return False
        if via_action.component_type == "tab_item":
            return False
        if via_action.shared_region == "bottom_tab":
            return False
        if (via_action.label or "").startswith("tab_"):
            return False
        return True

    def _click_target(self, target: ClickTarget, *, tag: str = "navigate") -> None:
        SLog.i(TAG, f"Click {tag} 80%: {target.label} @ {target.center}")
        if target.label and hasattr(self._engine, "click_by_label"):
            if self._engine.click_by_label(target.label):
                mSleep(1.5)
                self._after_gesture()
                return
        cx, cy = target.center
        click_fn = getattr(self._engine, "click", None)
        if click_fn:
            try:
                click_fn(None, position=(cx, cy), label=target.label or "")
            except TypeError:
                click_fn(None, position=(cx, cy))
        mSleep(1.5)
        self._after_gesture()

    def _discover_targets(
        self,
        sample_path: Optional[str],
        shots: Optional[List[np.ndarray]] = None,
    ) -> List[ClickTarget]:
        w, h = self._screen_size
        targets: List[ClickTarget] = []

        if shots:
            targets = navigation_targets_from_hotspots(
                discover_hotspots_from_frames(shots, w, h, max_items=MAX_CLICKS_PER_PAGE),
                screen_w=w,
                screen_h=h,
            )
        elif self._page_hotspots:
            targets = navigation_targets_from_hotspots(
                self._page_hotspots, screen_w=w, screen_h=h,
            )

        if len(targets) < 3:
            targets.extend(
                discover_clickables_from_hierarchy(self._engine, w, h, max_items=12)
            )
        if len(targets) < 4 and sample_path:
            targets.extend(discover_clickables_ocr(sample_path, w, h, max_items=12))

        deduped: List[ClickTarget] = []
        seen = set()
        for t in targets:
            key = (t.x // 12, t.y // 12, t.label, t.component_type)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(t)
        SLog.i(TAG, f"Navigation targets queued: {len(deduped)}")
        return deduped[:MAX_CLICKS_PER_PAGE]

    def crawl(
        self,
        *,
        persist=None,
        seed_node_id: Optional[str] = None,
        seed_label: str = "首页",
    ) -> CrawlReport:
        """
        BFS/DFS 跑图。persist 为 server.services.crawl_persistence 模块（可选）。
        """
        self._setup_device()
        self._launch_app()

        queue: List[Tuple[Optional[str], Optional[ClickTarget]]] = [(seed_node_id, None)]
        pages_done = 0
        seed_captured = False

        while queue and pages_done < self.max_pages:
            parent_id, via_action = queue.pop(0)

            if via_action and parent_id:
                edge_key = (parent_id, via_action.label, via_action.x // 12, via_action.y // 12)
                if edge_key in self._explored_edges:
                    continue
                self._explored_edges.add(edge_key)
            self._ensure_app_foreground()
            if via_action:
                self._click_target(via_action)
            else:
                self._action_index = 0
                self._explore_click_idx = 0
                self._left_page_recoveries = 0

            first = self._capture_gray()
            if first is None:
                self.report.errors.append("capture_failed")
                continue

            matched = self._match_known_page(first)
            if matched and via_action and parent_id and matched != parent_id:
                self.report.navigations.append(
                    NavRecord(
                        from_node_id=parent_id,
                        to_node_id=matched,
                        action={
                            "type": "click",
                            "label": via_action.label,
                            "x": via_action.x,
                            "y": via_action.y,
                            "w": via_action.w,
                            "h": via_action.h,
                            "source": via_action.source,
                        },
                        similarity_to_from=frame_similarity(first, self._known_pages.get(parent_id, first)),
                    )
                )
                if persist and parent_id:
                    persist.ensure_edge(
                        self.graph_id,
                        parent_id,
                        matched,
                        trigger=self.report.navigations[-1].action,
                        source_handle=via_action.label,
                        label=via_action.label,
                    )
                if self._needs_back_after_visit(via_action):
                    self._request_back()
                else:
                    self._ensure_app_foreground()
                continue

            if matched and via_action and parent_id and matched == parent_id:
                SLog.w(TAG, f"Click '{via_action.label}' stayed on page {parent_id}, skip")
                continue

            if not self._is_new_page(first) and not via_action:
                SLog.i(TAG, "Skip already visited page signature")
                continue

            node_id = seed_node_id if seed_node_id and not via_action else f"page-{uuid.uuid4().hex[:8]}"
            label = seed_label if not via_action else page_name_from_label(via_action.label)

            shots, err = self.collect_page_screenshots()
            if err == "left_page":
                SLog.w(TAG, f"Left page while collecting ({len(shots)} shots), relaunch app")
                self._ensure_app_foreground()
                if shots:
                    err = f"incomplete_{len(shots)}"
                else:
                    continue
            if err and err.startswith("incomplete"):
                SLog.w(TAG, f"Page {label}: {err}, saving partial")
            if not shots:
                self._ensure_app_foreground()
                continue

            ref = shots[0]
            self._known_pages[node_id] = ref
            self._visited_signatures.append(self._page_signature(ref))

            paths: List[str] = []
            static_names: List[str] = []
            if persist:
                for i, img in enumerate(shots):
                    url = persist.save_screenshot_file(img, prefix=f"crawl_{node_id[:8]}")
                    paths.append(url)
                    static_names.append(url)
                h_img, w_img = ref.shape[:2]
                persist.ensure_page_node(
                    self.graph_id,
                    node_id,
                    label,
                    screenshot=paths[0],
                    natural_size={"w": w_img, "h": h_img},
                    x=(pages_done % 4) * 420,
                    y=(pages_done // 4) * 520,
                )
                train_names = static_names if len(static_names) >= 2 else static_names * 2
                if train_names:
                    persist.train_skeleton_for_node(self.graph_id, node_id, train_names)

                if via_action and parent_id:
                    nav = NavRecord(
                        from_node_id=parent_id,
                        to_node_id=node_id,
                        action={
                            "type": "click",
                            "label": via_action.label,
                            "x": via_action.x,
                            "y": via_action.y,
                            "w": via_action.w,
                            "h": via_action.h,
                        },
                    )
                    self.report.navigations.append(nav)
                    persist.ensure_edge(
                        self.graph_id,
                        parent_id,
                        node_id,
                        trigger=nav.action,
                        label=via_action.label,
                    )
            else:
                paths = [f"memory://{node_id}/{i}" for i in range(len(shots))]

            self.report.pages.append(
                PageCaptureResult(
                    node_id=node_id,
                    label=label,
                    screenshot_paths=paths,
                    image_files=static_names,
                    natural_size={"w": ref.shape[1], "h": ref.shape[0]},
                )
            )
            pages_done += 1
            seed_node_id = None

            sample_path = None
            if persist and paths:
                from server.core.database import APP_DATA_DIR
                sample_path = os.path.join(
                    APP_DATA_DIR, "uploads", paths[0].split("/static/")[-1]
                )

            nav_targets = self._discover_targets(sample_path, shots=shots)
            for target in nav_targets:
                queue.append((node_id, target))

            if not seed_captured and not via_action:
                seed_captured = True
                SLog.i(TAG, f"Seed page saved, queued {len(nav_targets)} tab clicks")

            if via_action and self._needs_back_after_visit(via_action):
                self._request_back()
            else:
                self._ensure_app_foreground()

        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)
            out = os.path.join(self.output_dir, f"crawl_{self.graph_id}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "pages": [asdict(p) for p in self.report.pages],
                        "navigations": [asdict(n) for n in self.report.navigations],
                        "errors": self.report.errors,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            SLog.i(TAG, f"Crawl report written: {out}")

        return self.report
