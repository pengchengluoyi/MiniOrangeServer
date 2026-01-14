# driver/agent/Perception/Vision/mSceneMatching.py
import cv2
import numpy as np
import os
from script.log import SLog


class SceneMatcher:
    @staticmethod
    def verify_layout(current_img, node_data):
        """
        骨架提取与比对：使用投影算法 (Projection Profile)
        不看细节，只看布局 (Layout) 和红框区域的留白
        """
        ref_path = node_data.get("screenshot")
        if not ref_path or not os.path.exists(ref_path):
            # SLog.w("SceneMatcher", f"Reference screenshot missing: {ref_path}")
            return 0.0

        ref_img = cv2.imdecode(np.fromfile(ref_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if ref_img is None: return 0.0

        # 1. 应用蒙版 (Masking) - 涂黑动态区域 (红框区域)
        masks = node_data.get("mask_areas", [])
        
        def apply_mask(img, mask_list):
            masked = img.copy()
            for mask in mask_list:
                mx, my, mw, mh = map(int, mask['rect'])
                cv2.rectangle(masked, (mx, my), (mx + mw, my + mh), (0, 0, 0), -1)
            return masked

        curr_masked = apply_mask(current_img, masks)
        ref_masked = apply_mask(ref_img, masks)

        # Resize current to match reference
        h, w = ref_masked.shape[:2]
        curr_resized = cv2.resize(curr_masked, (w, h))

        # 2. 骨架提取 (Canny + Projection)
        def get_projection_signature(img):
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            
            # Vertical Projection (Sum of cols)
            v_proj = np.sum(edges, axis=0)
            # Horizontal Projection (Sum of rows)
            h_proj = np.sum(edges, axis=1)
            
            # Normalize
            v_proj = v_proj / (np.max(v_proj) + 1e-5)
            h_proj = h_proj / (np.max(h_proj) + 1e-5)
            return v_proj, h_proj

        ref_v, ref_h = get_projection_signature(ref_masked)
        curr_v, curr_h = get_projection_signature(curr_resized)

        # 3. Compare Correlation
        score_v = np.corrcoef(ref_v, curr_v)[0, 1]
        score_h = np.corrcoef(ref_h, curr_h)[0, 1]
        
        if np.isnan(score_v): score_v = 0
        if np.isnan(score_h): score_h = 0
        
        return max(0.0, (score_v + score_h) / 2)

    @staticmethod
    def detect_popup(current_img):
        # TODO: Implement popup detection logic
        return False