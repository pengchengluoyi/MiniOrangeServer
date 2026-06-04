# driver/agent/Planning/Page.py
from driver.agent.Memory import memory_manager
from driver.agent.Action.tools import Tool
from script.log import SLog
from script.sleep import mSleep
import builtins

TAG = "PageSOP"


class Page:
    @staticmethod
    def _execute_healing(solution):
        if solution and solution.get("type") == "dynamic_action":
            comp = solution.get("component")
            if comp:
                SLog.i(TAG, f"🚑 Executing healing action: Click [{comp.get('label')}]")
                try:
                    x = float(comp.get("x", 0))
                    y = float(comp.get("y", 0))
                    w = float(comp.get("width", 0))
                    h = float(comp.get("height", 0))
                    target_x = x + w / 2
                    target_y = y + h / 2
                    
                    Tool.gesture("click", [target_x, target_y])
                    mSleep(2.0)
                    return True
                except Exception as e:
                    SLog.e(TAG, f"❌ Healing action failed: {e}")
        return False

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
            return True

        # 2. 在图谱中查找当前动作节点的“出边” (Source -> Target)
        # 假设 current_node.id 对应图谱中的 source
        edges = app_graph.get("edges", [])
        target_id = None
        for edge in edges:
            if edge["source"] == current_node.id:
                target_id = edge["target"]
                break

        if not target_id:
            return True

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
                identified, id_score = feedback.identify_current_page(app_graph)
                if identified:
                    SLog.w(
                        TAG,
                        f"⚠️ Visual verification failed! Expected: {target_node.get('label')}, "
                        f"but screen looks like: {identified.get('label')} ({id_score:.2f})",
                    )
                else:
                    SLog.w(TAG, f"⚠️ Visual verification failed! Expected: {target_node.get('label')}")
                
                # 尝试自愈
                solution = Page.find_solution(feedback, current_node.id)
                if Page._execute_healing(solution):
                    # 自愈后再次核验
                    if feedback.verify_current_page(target_node):
                        SLog.i(TAG, "✅ Healing successful: Target page reached.")
                        return True
                
                return False
        return True

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
        if not app_graph: return True

        edges = app_graph.get("edges", [])
        # 找到当前启动节点的下一个节点 ID
        target_id = next((e["target"] for e in edges if e["source"] == current_node.id), None)
        if not target_id: return True

        nodes = app_graph.get("nodes", [])
        target_node = next((n for n in nodes if n["id"] == target_id), None)

        if not target_node or target_node.get("type") != "page":
            SLog.w(TAG, "⚠️ 启动节点后未连接页面节点，无法执行智能等待，使用默认延时。")
            mSleep(5.0)
            return True

        # 2. 轮询检测 (最多等待 20秒)
        for i in range(20):
            if feedback.verify_current_page(target_node):
                SLog.i(TAG, "✅ 应用启动确认: 已检测到首页。")
                return True

            # 简单的自愈尝试 (针对开屏广告等)
            if i > 5 and i % 3 == 0:
                solution = Page.find_solution(feedback, current_node.id)
                Page._execute_healing(solution)

            SLog.d(TAG, f"⏳ 等待应用首页渲染... ({i + 1}/20)")
            mSleep(1.0)

        SLog.e(TAG, "❌ 应用启动超时: 未能检测到首页特征。")
        return False

    @staticmethod
    def find_solution(feedback, current_node_id):
        """
        🔥 智能预测与自愈 (Predictive Healing)
        不再依赖手动配置的 SOP 优先级，而是基于全图谱的相似度匹配。
        "从一堆脚本中推算出来哪个 workflow 的哪几个步骤能解决当前问题"
        """
        SLog.i(TAG, "🚑 任务受阻，启动 [群体智慧] 诊断程序...")
        
        app_graph = memory_manager.long_term.app_graph
        if not app_graph: return None
        graph_id = app_graph.get("id") # 假设 graph 元数据里有 id

        # 1. 感知 (Perceive): 获取当前屏幕的关键文本特征
        # 这里假设 feedback 提供了 OCR 结果
        # 实际应提取当前屏幕所有可见文本
        current_texts = feedback.get_all_texts() 
        if not current_texts:
            SLog.w(TAG, "当前屏幕无文本特征，无法进行匹配")
            return None

        # 2. 询问服务端 (Query Hive Mind)
        # 调用我们在 wAppGraph.py 中写的新接口
        if hasattr(builtins, "SERVER_QUERY"):
            response = builtins.SERVER_QUERY("app_graph/match_solution", {
                "graph_id": graph_id,
                "texts": current_texts
            })
            
            if response and response.get("code") == 200:
                recommendations = response.get("data", [])
                
                if recommendations:
                    # 取置信度最高的一个
                    best_rec = recommendations[0]
                    score = best_rec.get("score", 0)
                    action = best_rec.get("action")
                    comp = best_rec.get("component")
                    
                    SLog.i(TAG, f"✅ 预测成功 (置信度 {score:.2f}): 建议点击 [{comp.get('label')}]")
                    
                    # 这里我们返回组件信息，而不是 Workflow ID
                    # 上层调用者需要能够处理这种动态的 Action，而不仅仅是跑一个 Workflow
                    # 为了兼容旧逻辑，我们可以临时构造一个微型 Workflow
                    return {"type": "dynamic_action", "component": comp}
        
        SLog.w(TAG, "🤷‍♂️ 预测结束: 知识库中未找到类似场景")
        return None
