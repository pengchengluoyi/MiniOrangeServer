# /driver/agent/Perception/Vision/feedback.py

from driver.agent.Perception.Vision.mImageMatching import ImageVision
from driver.agent.Perception.Vision.mSceneMatching import SceneMatcher
from driver.agent.Perception.Vision.mOcr import check_anchors
from script.log import SLog


class Feedback:
    def verify_current_page(self, node_data):
        """
        视觉判断中心：总指挥
        1. 调用 mImageMatching 拿到去噪图
        2. 调用 mSceneMatching 问：“有弹窗吗？”、“骨架对吗？”
        3. 调用 mOcr 问：“关键文字在吗？”
        4. 计算加权总分
        """
        page_label = node_data.get('label', 'Unknown')
        SLog.d("Feedback", f"🔍 Verifying page: {page_label}")
        
        # 1. Image Pre-processing (Temporal Denoising)
        golden_frame = ImageVision.get_golden_frame(count=3)
        if golden_frame is None:
            SLog.e("Feedback", "❌ 无法获取屏幕图像流，请检查设备是否锁屏、黑屏或连接中断。")
            return False
            
        # 2. Scene Understanding (Layout & Popup)
        layout_score = SceneMatcher.verify_layout(golden_frame, node_data)
        has_popup = SceneMatcher.detect_popup(golden_frame)
        
        # 3. Text Perception (Anchors)
        anchors = node_data.get('anchors', [])
        anchor_score = 1.0
        if anchors:
            anchor_score = check_anchors(golden_frame, anchors)
            
        # 4. Weighted Decision (Layout 60% + Anchors 40%)
        final_score = (0.6 * layout_score) + (0.4 * anchor_score)
        
        is_match = final_score >= 0.7
        status_icon = "✅" if is_match else "❌"
        SLog.i("Feedback", f"{status_icon} Verification Result: {final_score:.2f} (Layout: {layout_score:.2f}, Anchors: {anchor_score:.2f})")
        
        return is_match