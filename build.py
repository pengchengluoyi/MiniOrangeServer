import os
import shutil
import subprocess
import sys
import re


# =============================================================================
# 1. 基础工具函数 (保持你原有的逻辑)
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


def get_requirements_libs():
    """读取 requirements.txt 并提取纯包名"""
    libs = []
    if os.path.exists("requirements.txt"):
        with open("requirements.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                # 提取包名 (例如 'rapidocr_onnxruntime==1.0' -> 'rapidocr_onnxruntime')
                name = re.split(r'[<>=!~;@\s]', line)[0]
                if name and name.lower() not in ['pip', 'setuptools', 'wheel', 'pyinstaller']:
                    libs.append(name)
    print(f"--- Libraries to collect: {libs} ---")
    return libs


# =============================================================================
# 2. 构建主逻辑
# =============================================================================

def build():
    version = get_version()
    dist_name = f"MiniOrangeServer_v{version}"
    req_libs = get_requirements_libs()

    # 将列表转为字符串注入到 spec 文件
    req_libs_str = str(req_libs)

    print(f"--- 1. Cleaning up old builds (Target: {dist_name}) ---")
    clean(['build', 'dist', 'main.spec'])

    print("--- 2. Generating main.spec ---")

    # 注意：我们在这里注入了一个强大的辅助函数 collect_package_deeply
    # 它会找到包的物理路径，并把整个文件夹作为资源打包，解决 config.yaml 丢失问题

    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import os
import importlib.util
import sys

block_cipher = None

# --- 来自 build.py 的 requirements 列表 ---
requirements_libs = {req_libs_str}

datas = []
binaries = []
hiddenimports = []

# =============================================================================
# 🔥 核心增强：全量深度收集函数
# =============================================================================
def collect_package_deeply(package_name):
    '''
    暴力收集：找到包的安装目录，将其所有内容（包含 yaml, json, dll 等）
    都添加到 datas 中，确保任何配置文件都不会丢失。
    '''
    d_list = []
    b_list = []
    h_list = []

    try:
        # 1. 尝试使用 PyInstaller 标准收集 (获取依赖、dll 等)
        # 这步是为了保证基础的二进制依赖被识别
        try:
            tmp = collect_all(package_name)
            d_list += tmp[0]
            b_list += tmp[1]
            h_list += tmp[2]
        except Exception:
            pass # 有些包没有 hook，忽略错误

        # 2. 深度资源扫描 (解决 config.yaml 等丢失的关键)
        spec = importlib.util.find_spec(package_name)
        if spec and spec.origin:
            # 获取包的根目录 (例如 .../site-packages/rapidocr_onnxruntime)
            pkg_path = os.path.dirname(spec.origin)

            # 只有当它是一个目录时才处理 (排除单文件模块)
            if os.path.isdir(pkg_path):
                print(f"  -> Deep collecting resources for: {{package_name}}")
                # 语法: (本地源路径, 打包后的目标目录名)
                # 这样打包后，程序运行时能在 _internal/package_name/ 下找到所有原文件
                d_list.append((pkg_path, package_name))

    except Exception as e:
        print(f"  -> Warning: Could not deeply collect {{package_name}}: {{e}}")

    return d_list, b_list, h_list

# =============================================================================
# A. 循环处理所有依赖 (All-in-One Collection)
# =============================================================================
print("--- Starting Comprehensive Dependency Collection ---")

# 1. 先对 requirements 里的每个包进行“深度收集”
for lib in requirements_libs:
    d, b, h = collect_package_deeply(lib)
    datas += d
    binaries += b
    hiddenimports += h

# 2. 显式补充关键库 (虽然上面循环了，但为了保险起见，保留核心库的显式声明)
# 尤其是 cv2，容易出玄学问题
try:
    tmp_cv2 = collect_all('cv2')
    datas += tmp_cv2[0]
    binaries += tmp_cv2[1]
    hiddenimports += tmp_cv2[2]
except: 
    pass

# 3. 补充 comtypes 和 uiautomation (Windows 自动化核心)
hiddenimports += ['comtypes', 'comtypes.gen', 'comtypes.stream', 'uiautomation']

# 4. 补充隐式依赖
hiddenimports += [
    'onnx', 
    'uiautomator2.core',
    'pkg_resources.extern',
    'uvicorn.loops.auto',
    'uvicorn.protocols.http.auto',
    'uvicorn.lifespan.on',
]

# =============================================================================
# B. 本地源码与资源收集
# =============================================================================

# 1. 收集项目根目录下的 resource 文件夹
if os.path.exists('resource'):
    datas.append(('resource', 'resource'))

# 2. 收集本地 Python 源码模块 (ability, server, driver 等)
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

hiddenimports += find_local_modules('ability')
hiddenimports += find_local_modules('server')
hiddenimports += find_local_modules('script')
hiddenimports += find_local_modules('driver')

# 3. 去重
hiddenimports = list(set(hiddenimports))

# =============================================================================
# C. PyInstaller 配置对象
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
    excludes=[], # 如果需要减小体积，可在此排除不需要的库
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