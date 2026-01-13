import os
import shutil
import subprocess
import sys
import re


# =============================================================================
# 1. 基础工具函数
# =============================================================================

def clean(targets):
    """清理指定的文件或目录"""
    for target in targets:
        if os.path.isfile(target):
            try:
                os.remove(target)
            except Exception as e:
                print(f"Error removing {target}: {e}")
        elif os.path.isdir(target):
            try:
                shutil.rmtree(target)
            except Exception as e:
                print(f"Error removing {target}: {e}")


def get_version():
    """从 main.py 中提取版本号"""
    try:
        with open("main.py", "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'"version":\s*"([^"]+)"', content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "1.0.0"


# =============================================================================
# 2. 构建主逻辑
# =============================================================================

def build():
    version = get_version()
    dist_name = f"MiniOrangeServer_v{version}"

    print(f"--- 1. Cleaning up old builds (Target: {dist_name}) ---")
    clean(['build', 'dist', 'main.spec'])

    print("--- 2. Generating main.spec ---")

    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import os
import importlib.util
import sys

block_cipher = None

datas = []
binaries = []
hiddenimports = []

# =============================================================================
# A. 暴力修复区：针对那些配置文件必定丢失的库
# =============================================================================
def force_deep_collect(package_name):
    '''
    暴力收集：直接拷贝包的物理安装目录。
    仅用于 RapidOCR 这种普通方法无法收集配置文件的库。
    '''
    d_list = []
    try:
        spec = importlib.util.find_spec(package_name)
        if spec and spec.origin:
            pkg_path = os.path.dirname(spec.origin)
            if os.path.isdir(pkg_path):
                print(f"  -> [Hard Copy] Collecting source for: {{package_name}}")
                d_list.append((pkg_path, package_name))
    except Exception as e:
        print(f"  -> Warning: Could not collect {{package_name}}: {{e}}")
    return d_list

# 🔥 RapidOCR 必须暴力拷贝，否则 config.yaml 会丢
datas += force_deep_collect('rapidocr_onnxruntime')


# =============================================================================
# B. 核心依赖全名单 (使用智能收集 collect_all)
# =============================================================================
# 这里列出了你项目里所有可能用到的“重型”库。
# collect_all 会自动分析依赖，比暴力拷贝快，但比默认打包全。

full_libs_list = [
    # 1. 基础自动化
    'uiautomator2', 
    'uiautomation', 
    'cv2', 
    'PIL',            # Pillow
    'numpy',
    'zeroconf', 
    'websockets',

    # 2. AI 与 大模型相关 (你刚才反馈缺失的部分)
    'torch',          # PyTorch 核心
    'accelerate',     # HuggingFace Accelerate
    'transformers',   # HuggingFace Transformers
    'qwen_vl_utils',  # Qwen VL 工具
    'onnxruntime',    # ONNX 推理
    'torchvision',    # (可选) 如果用到图像处理通常都需要
    'pyclipper',      # RapidOCR 依赖 (必须显式收集，因为是动态引用的 C 扩展)
    'shapely',        # RapidOCR 依赖 (几何计算库)
    'fastapi',        # Web 服务端核心 (必须显式收集)
    'uvicorn',        # ASGI 服务器
    'sqlalchemy',     # 数据库 ORM
    'starlette',      # FastAPI 依赖
    'pydantic',       # 数据验证
    'tiktoken',       # 大模型 Tokenizer (Qwen/OpenAI 依赖)
    'sentencepiece',  # 大模型 Tokenizer (Llama/HF 依赖)
    'ultralytics',    # YOLO 视觉模型 (包含大量配置和数据文件，必须收集)
    'adbutils',       # ADB 通信库 (纯 Python 实现的 ADB 协议)
    'pandas',         # 数据分析 (包含大量 C 扩展)
    'scipy',          # 科学计算
    'matplotlib',     # 绘图库
    'pywinauto',      # Windows 自动化
    'apscheduler',    # 定时任务调度 (requirements 中有)
    'selenium',       # Web 自动化 (requirements 中有)
    'paddleocr',      # PaddleOCR (requirements 中有)
    'paddle',         # PaddlePaddle (requirements 中有)
    'ruamel.yaml',    # YAML 处理
    'coloredlogs',    # 日志美化
    'humanfriendly',  # 日志依赖
    'Crypto',         # pycryptodome 加密
    'ujson',          # 高性能 JSON
    'trio',           # 异步 I/O (Selenium 依赖)
    'trio_websocket', # WebSocket (Selenium 依赖)
    'psutil',         # 系统信息 (用于精准获取局域网 IP)
]

print("--- Collecting comprehensive libraries (Smart Mode) ---")
for lib in full_libs_list:
    try:
        # print(f"  -> Analyzing: {{lib}}") # 如果想看进度可以取消注释
        tmp = collect_all(lib)
        datas += tmp[0]
        binaries += tmp[1]
        hiddenimports += tmp[2]
    except Exception as e:
        # 很多库是可选的，比如没装 torchvision 报错忽略即可
        pass 

# =============================================================================
# C. 补充隐式 Hidden Imports
# =============================================================================
hiddenimports += [
    # Windows COM 接口
    'comtypes', 
    'comtypes.gen', 
    'comtypes.stream',

    # 基础依赖补漏
    'onnx',
    'uiautomator2.core',
    'pkg_resources.extern',

    # FastAPI / Uvicorn 服务器组件
    'uvicorn.loops.auto',
    'uvicorn.protocols.http.auto',
    'uvicorn.lifespan.on',

    # HuggingFace / Torch 常见隐式调用
    'tqdm',
    'regex',
    'requests',
    'packaging',
    'packaging.version',
    'packaging.specifiers',
    'packaging.requirements',
    'yaml',           # PyYAML (RapidOCR 读取 config.yaml 需要)
    'python-multipart', # FastAPI 处理文件上传必须的隐式依赖 (wsFile.py 用到了上传)
    'multiprocessing',  # 多进程支持
]

# =============================================================================
# D. 本地代码全量扫描 (防止业务代码丢失)
# =============================================================================

# 1. 资源目录
if os.path.exists('resource'):
    datas.append(('resource', 'resource'))

# 2. 源码目录扫描
def find_local_modules(root_dir):
    modules = []
    if not os.path.exists(root_dir): return modules
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith(".py") and file != "__init__.py":
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, ".")
                mod_name = rel_path.replace(os.sep, ".")[:-3]
                modules.append(mod_name)
    return modules

# 扫描所有业务文件夹
hiddenimports += find_local_modules('ability')
hiddenimports += find_local_modules('server')
hiddenimports += find_local_modules('script')
hiddenimports += find_local_modules('driver')

# 去重
hiddenimports = list(set(hiddenimports))

# =============================================================================
# E. PyInstaller 配置
# =============================================================================
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[], 
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MiniOrangeServer',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='{dist_name}',
)
"""

    with open("main.spec", "w", encoding="utf-8") as f:
        f.write(spec_content)

    print("--- 3. Running PyInstaller ---")
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "main.spec"])

    print("--- 4. Cleaning temp files ---")
    clean(['build', 'main.spec'])
    print("--- Build Complete! ---")


if __name__ == "__main__":
    build()