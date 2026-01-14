# driver/agent/Planning/Page.py
from driver.agent.Memory import memory_manager
from script.log import SLog
from script.sleep import mSleep

TAG = "PageSOP"


class Page:
    @staticmethod
    def verify_step(feedback, current_node):
        """
        SOP: 步骤验证
        执行完动作后，查询应用图谱，找到预期的下一个页面节点，并进行视觉核验
        """

        memory_manager.short_term.set_timeline_scope("planning", {"sop": "verify_step"})
        # 1. 获取应用图谱
        app_graph = memory_manager.long_term.app_graph
        if not app_graph:
            return

        # 2. 在图谱中查找当前动作节点的“出边” (Source -> Target)
        # 假设 current_node.id 对应图谱中的 source
        edges = app_graph.get("edges", [])
        target_id = None
        for edge in edges:
            if edge["source"] == current_node.id:
                target_id = edge["target"]
                break

        if not target_id:
            return

        # 3. 获取目标节点详情
        nodes = app_graph.get("nodes", [])
        target_node = next((n for n in nodes if n["id"] == target_id), None)

        # 4. 如果目标是“页面”类型，则启动视觉核验
        if target_node and target_node.get("type") == "page":
            SLog.d(TAG, f"Expecting transition to page: {target_node.get('label')}")

            # 给一点时间让页面渲染 (Feedback 内部也会去噪等待，但这里可以预留一点缓冲)
            mSleep(1.0)

            # 调用视觉皮层进行核验
            is_match = feedback.verify_current_page(target_node)

            if not is_match:
                SLog.w(TAG, f"⚠️ Visual verification failed! Expected: {target_node.get('label')}")
                # TODO: 这里可以触发重试机制、错误上报或“迷路”后的自动恢复逻辑

    @staticmethod
    def wait_for_app_launch(feedback, current_node):
        """
        SOP: 应用启动等待
        应用启动专用等待策略：
        1. 智能轮询直到检测到下一页 (首页)
        2. 超时保护
        """
        SLog.i(TAG, "🚀 应用启动中... 正在等待首页加载...")
        memory_manager.short_term.set_timeline_scope("planning", {"sop": "wait_for_app_launch"})

        # 1. 查找预期的“首页”节点
        app_graph = memory_manager.long_term.app_graph
        if not app_graph: return

        edges = app_graph.get("edges", [])
        # 找到当前启动节点的下一个节点 ID
        target_id = next((e["target"] for e in edges if e["source"] == current_node.id), None)
        if not target_id: return

        nodes = app_graph.get("nodes", [])
        target_node = next((n for n in nodes if n["id"] == target_id), None)

        if not target_node or target_node.get("type") != "page":
            SLog.w(TAG, "⚠️ 启动节点后未连接页面节点，无法执行智能等待，使用默认延时。")
            mSleep(5.0)
            return

        # 2. 轮询检测 (最多等待 20秒)
        for i in range(20):
            if feedback.verify_current_page(target_node):
                SLog.i(TAG, "✅ 应用启动确认: 已检测到首页。")
                return

            SLog.d(TAG, f"⏳ 等待应用首页渲染... ({i + 1}/20)")
            mSleep(1.0)

        SLog.e(TAG, "❌ 应用启动超时: 未能检测到首页特征。")
