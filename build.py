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

def build():
    version = get_version()
    dist_name = f"MiniOrangeServer_v{version}"
    print(f"--- 1. Cleaning up old builds (Target: {dist_name}) ---")
    clean(['build', 'dist', 'main.spec'])

    print("--- 2. Generating main.spec ---")
    # 使用 raw string (r"...") 避免转义问题
    spec_content = r"""# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import os

block_cipher = None

# --- A. 自动收集第三方库资源 (uiautomator2) ---
tmp_ret = collect_all('uiautomator2')
datas = tmp_ret[0]
binaries = tmp_ret[1]
hiddenimports = tmp_ret[2]

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
                # 转换为模块名 (例如 ability.component.mobile.click)
                # os.sep 会自动适配 Windows(\) 和 Mac(/)
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

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 🔥 优化：排除不必要的重型库，减少文件数量，加快 Electron 签名速度
    # 如果你的项目没用到 PyTorch，排除它可以减少几百 MB 体积和数千个文件
    excludes=['torch', 'torchvision', 'torchaudio'], 
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
    name='""" + dist_name + r"""',
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