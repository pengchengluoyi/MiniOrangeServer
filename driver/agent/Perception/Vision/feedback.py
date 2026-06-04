# /driver/agent/Perception/Vision/feedback.py

from driver.agent.Perception.Vision.mImageMatching import ImageVision
from driver.agent.Perception.Vision.mSceneMatching import SceneMatcher
from driver.agent.Perception.Vision.mOcr import check_anchors
from server.core.vision.skeleton_algo import SkeletonAlgo
from script.log import SLog


class Feedback:
    SKELETON_MATCH_THRESHOLD = 0.75

    def _layout_score(self, golden_frame, node_data):
        sk = node_data.get("skeleton_config") or {}
        master_path, mask_path, _ = SkeletonAlgo.skeleton_config_paths(sk)
        if master_path and mask_path:
            score = SkeletonAlgo.score_node_match(golden_frame, node_data)
            SLog.d("Feedback", f"Skeleton layout score: {score:.2f}")
            return score
        return SceneMatcher.verify_layout(golden_frame, node_data)

    def verify_current_page(self, node_data):
        """
        视觉判断中心：总指挥
        1. 调用 mImageMatching 拿到去噪图
        2. 优先用训练好的页面骨架蒙版比对；否则回退投影布局匹配
        3. 调用 mOcr 问：“关键文字在吗？”
        4. 计算加权总分
        """
        page_label = node_data.get('label', 'Unknown')
        SLog.d("Feedback", f"🔍 Verifying page: {page_label}")

        golden_frame = ImageVision.get_golden_frame(count=3)
        if golden_frame is None:
            SLog.e("Feedback", "❌ 无法获取屏幕图像流，请检查设备是否锁屏、黑屏或连接中断。")
            return False

        layout_score = self._layout_score(golden_frame, node_data)
        has_popup = SceneMatcher.detect_popup(golden_frame)

        anchors = node_data.get('anchors', [])
        anchor_score = 1.0
        if anchors:
            anchor_score = check_anchors(golden_frame, anchors)

        final_score = (0.6 * layout_score) + (0.4 * anchor_score)

        is_match = final_score >= 0.8
        status_icon = "✅" if is_match else "❌"
        SLog.i(
            "Feedback",
            f"{status_icon} Verification Result: {final_score:.2f} "
            f"(Layout: {layout_score:.2f}, Anchors: {anchor_score:.2f}, Popup: {has_popup})",
        )

        return is_match

    def identify_current_page(self, app_graph, min_score=None):
        """
        在整张应用图谱中，用各页面的骨架模型识别当前屏幕所在页面。
        返回 (node_dict, confidence) 或 (None, 0.0)
        """
        min_score = self.SKELETON_MATCH_THRESHOLD if min_score is None else min_score
        if not app_graph:
            return None, 0.0

        golden_frame = ImageVision.get_golden_frame(count=3)
        if golden_frame is None:
            return None, 0.0

        candidates = []
        for node in app_graph.get("nodes", []):
            if node.get("type") != "page":
                continue
            sk = node.get("skeleton_config") or {}
            master_path, mask_path, ignored_areas = SkeletonAlgo.skeleton_config_paths(sk)
            if not master_path or not mask_path:
                continue
            candidates.append({
                "id": node.get("id"),
                "label": node.get("label"),
                "master_path": master_path,
                "mask_path": mask_path,
                "ignored_areas": ignored_areas,
                "_node": node,
            })

        if not candidates:
            SLog.w("Feedback", "No page skeleton models available for identification")
            return None, 0.0

        best, score = SkeletonAlgo.identify_page_from_image(golden_frame, candidates)
        if not best or score < min_score:
            SLog.d("Feedback", f"Page identification below threshold: {score:.2f}")
            return None, score

        node = best.get("_node")
        SLog.i("Feedback", f"📍 Identified page: {best.get('label')} ({score:.2f})")
        return node, score
