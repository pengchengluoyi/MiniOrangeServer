# driver/agent/Planning/device_doctor.py
import builtins
from driver.agent.Memory import memory_manager

class DeviceDoctor:
    """
    职责：提供设备健康检查的标准作业程序 (SOP)。
    它不执行动作，只告诉中控台：
    1. 你该检查什么？(Check Target)
    2. 如果检查不通过，你该按什么顺序执行哪些动作？(Recovery Plan)
    """

    @staticmethod
    def get_health_check_sop():
        """
        生成设备自检 SOP
        """
        return [
            # --- SOP 步骤 1: 检查屏幕是否点亮 ---
            {
                "name": "screen_status",
                "check_logic": "is_screen_on",  # 告诉 Orchestrator 去问 Perception 这个问题
                "description": "检查屏幕是否为黑屏",
                "remedy_plan": [  # 如果检查失败，执行这套动作
                    {
                        "tool": "keyevent",
                        "args": {"key_code": "26"},  # 26 is POWER
                        "desc": "按下电源键唤醒"
                    },
                    {
                        "tool": "wait",
                        "args": {"seconds": 1.5},
                        "desc": "等待屏幕响应"
                    },
                    {
                        "tool": "gesture",  # 双击唤醒作为备选，或者这里简化处理
                        "args": {
                            "mtype": "double_tap",
                            "position": [500, 1000]  # 假设中心点
                        },
                        "desc": "尝试双击唤醒"
                    }
                ]
            },
            # --- SOP 步骤 2: 检查是否锁定 ---
            {
                "name": "lock_status",
                "check_logic": "is_unlocked",  # 注意这里逻辑是：期望“未锁定”
                "description": "检查屏幕是否在锁屏界面",
                "remedy_plan": [
                    {
                        "tool": "gesture",
                        "args": {
                            "mtype": "swipe",
                            "position": [500, 1800, 500, 500]  # 上滑动作
                        },
                        "desc": "上滑呼出密码盘"
                    },
                    {
                        "tool": "wait",
                        "args": {"seconds": 1.0},
                        "desc": "等待动画"
                    },
                    {
                        "tool": "input_password",  # 特殊指令，指示 Orchestrator 去记忆模块取密码并输入
                        "args": {},
                        "desc": "输入设备密码"
                    },
                    {
                        "tool": "keyevent",
                        "args": {"key_code": "66"},  # 66 is ENTER
                        "desc": "确认输入"
                    }
                ]
            }
        ]

    @staticmethod
    def unlock_sop():
        TARGET_DEVICE_SN = getattr(builtins, "TARGET_DEVICE_SN", None)
        password = memory_manager.short_term.get_global(f"{TARGET_DEVICE_SN}_password")

        memory_manager.short_term.set_timeline_scope("planning", {"sop": "unlock"})
        return [
            {
                "tool": "keyevent",
                "args": {
                    "key_code": "POWER"  # POWER
                }
            },
            {
                "tool": "input_text",
                "args": {
                    "text": f"{password}"
                }
            }
        ]
