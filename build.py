import os
import shutil
import subprocess
import sys
import re


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
        # 匹配 "version": "1.0.2"
        match = re.search(r'"version":\s*"([^"]+)"', content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "1.0.0"


def get_requirements_libs():
    """
    读取 requirements.txt 并提取包名
    例如: 'numpy==1.2.3' -> 'numpy'
    """
    libs = []
    req_file = "requirements.txt"
    if not os.path.exists(req_file):
        print(f"Warning: {req_file} not found. Skipping auto-collection.")
        return libs

    try:
        with open(req_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # 跳过空行和注释
                if not line or line.startswith('#'):
                    continue

                # 处理类似 "numpy>=1.20" 或 "requests==2.0" 的情况
                # 使用正则分割：空格, ==, >=, <=, >, <, ;, @
                # 取分割后的第一部分作为包名
                package_name = re.split(r'[<>=!~;@\s]', line)[0]

                if package_name:
                    # 可以在这里排除一些不需要收集的特殊包，例如 pip 本身
                    if package_name.lower() not in ['pip', 'setuptools', 'wheel']:
                        libs.append(package_name)
        print(f"--- Found {len(libs)} libraries in requirements.txt ---")
        return libs
    except Exception as e:
        print(f"Error parsing requirements.txt: {e}")
        return []


def build():
    version = get_version()
    # 获取 requirements 中的所有包名列表
    req_libs = get_requirements_libs()

    dist_name = f"MiniOrangeServer_v{version}"
    print(f"--- 1. Cleaning up old builds (Target: {dist_name}) ---")
    clean(['build', 'dist', 'main.spec'])

    print("--- 2. Generating main.spec ---")

    # 将 Python 列表转换为字符串形式，以便嵌入到 spec 文件的文本中
    # 结果类似于: "['numpy', 'requests', 'pandas']"
    req_libs_str = str(req_libs)

    # 使用 raw string (r"...") 避免转义问题
    # 注意：我们在 spec_content 中使用了 f-string (f""") 来注入变量
    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import os

block_cipher = None

# --- 定义从 requirements.txt 读取到的库列表 ---
requirements_libs = {req_libs_str}

datas = []
binaries = []
hiddenimports = []

# --- A. 自动循环收集所有依赖库 (核心修改) ---
# 遍历 requirements.txt 中的每个包，尝试收集其资源
print("--- Auto-collecting libraries from requirements.txt ---")
for lib in requirements_libs:
    try:
        # print(f"Processing: {{lib}}") # 调试用
        tmp_ret = collect_all(lib)
        datas += tmp_ret[0]
        binaries += tmp_ret[1]
        hiddenimports += tmp_ret[2]
    except Exception as e:
        # 某些包可能无法被 collect_all 识别（例如纯源码包），忽略错误继续
        # print(f"Skipping collect_all for {{lib}}: {{e}}")
        pass

# --- A2. 显式补充特定库 (兜底策略) ---
# 虽然上面循环了，但为了保险，保留原有的关键库处理逻辑，
# 特别是 cv2 这种容易出问题的库，或者需要手动指定位置的资源。

# --- A3. 强制收集 cv2 (OpenCV) ---
try:
    tmp_ret_cv2 = collect_all('cv2')
    datas += tmp_ret_cv2[0]
    binaries += tmp_ret_cv2[1]
    hiddenimports += tmp_ret_cv2[2]
except Exception:
    pass

# --- A5. 确保 uiautomation, comtypes 完整收集 (增强版) ---
try:
    # 推荐对 uiautomation 也使用自动收集
    tmp_ret_uia = collect_all('uiautomation')
    datas += tmp_ret_uia[0]
    binaries += tmp_ret_uia[1]
    hiddenimports += tmp_ret_uia[2]

    # 显式补强 comtypes 依赖
    hiddenimports += [
        'comtypes',
        'comtypes.gen',
        'comtypes.stream',
    ]
except Exception as e:
    print(f"Warning: Failed to collect uiautomation dependencies: {{e}}")

# --- A6. 补充缺失的隐式依赖 ---
hiddenimports += [
    'onnx', 
    'comtypes.gen',
    'uiautomator2.core',
    'pkg_resources.extern', # 解决日志中的 pkg_resources 警告
]

# --- A7. 递归收集整个 resource 目录 ---
if os.path.exists('resource'):
    # 第一个 'resource' 是你电脑上的文件夹名
    # 第二个 'resource' 是打包后在程序根目录生成的文件夹名
    datas.append(('resource', 'resource'))

# --- B. 辅助函数：递归收集本地源码模块 ---
# 解决 importlib 动态导入无法被 PyInstaller 识别的问题
def find_local_modules(root_dir):
    modules = []
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith(".py") and file != "__init__.py":
                # 拼接完整路径
                full_path = os.path.join(root, file)
                # 获取相对路径 (例如 ability/component/mobile/click.py)
                rel_path = os.path.relpath(full_path, ".")
                # 转换为模块名 (例如 driver.tentacle.component.mobile.click)
                # os.sep 会自动适配 Windows(\\) 和 Mac(/)
                mod_name = rel_path.replace(os.sep, ".")[:-3]
                modules.append(mod_name)
    return modules

# --- C. 显式加入所有业务代码模块 ---
hiddenimports += find_local_modules('ability')
hiddenimports += find_local_modules('server')
hiddenimports += find_local_modules('script')
hiddenimports += find_local_modules('driver')

# --- D. 加入 uvicorn 等隐式依赖 ---
hiddenimports += [
    'uvicorn.loops.auto',
    'uvicorn.protocols.http.auto',
    'uvicorn.lifespan.on',
]

# 去重
hiddenimports = list(set(hiddenimports))

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
    # 使用 sys.executable 确保调用的是当前虚拟环境中的 PyInstaller
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "main.spec"])

    print("--- 4. Cleaning temp files ---")
    clean(['build', 'main.spec'])
    print("--- Build Complete! ---")


if __name__ == "__main__":
    build()