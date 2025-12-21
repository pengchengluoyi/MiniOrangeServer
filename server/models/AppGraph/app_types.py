# server/app/models/app_types.py
from enum import Enum

class NodeType(str, Enum):
    PAGE = "page"           # 完整页面
    MODAL = "modal"         # 弹窗
    DRAWER = "drawer"       # 抽屉
    POPOVER = "popover"     # 气泡/悬浮层
    TOAST = "toast"         # 轻提示
    SECTION = "section"     # 局部区块

class TriggerType(str, Enum):
    CLICK = "click"
    HOVER = "hover"
    SCROLL = "scroll"
    LONG_PRESS = "long_press"
    TIMEOUT = "timeout"
    DRAG = "drag"

class ComponentCategory(str, Enum):
    INPUT = "input"         # 输入类
    SELECTION = "selection" # 选择类
    ACTION = "action"       # 动作类
    DISPLAY = "display"     # 展示类
    LAYOUT = "layout"       # 布局类
    FEEDBACK = "feedback"   # 反馈类
    # 🔥 高级能力
    HARDWARE = "hardware"   # 硬件调用
    SYSTEM = "system"       # 系统能力
    LBS = "lbs"             # 位置服务
    # 🔥 跨界与黑盒
    NAVIGATION = "navigation"
    EDITOR = "editor"

class InputType(str, Enum):
    TEXT = "text"; NUMBER = "number"; PASSWORD = "password"; EMAIL = "email"
    URL = "url"; SEARCH = "search"; TEXTAREA = "textarea"; DATE = "date"
    FILE = "file"; HIDDEN = "hidden"

class HardwareType(str, Enum):
    CAMERA = "camera_capture"; SCANNER = "qr_scanner"; MIC = "microphone"
    BIOMETRIC = "biometric"; NFC = "nfc"; BLUETOOTH = "bluetooth"

class NavigationType(str, Enum):
    INTERNAL = "internal"; EXTERNAL_LINK = "external_link"
    DEEP_LINK = "deep_link"; MINI_PROGRAM = "mini_program"

class EditorType(str, Enum):
    CANVAS = "canvas"; RICH_TEXT = "rich_text"; CODE = "code_editor"