#!/bin/bash

# 1. 清理旧构建
rm -rf build/ dist/ main.spec

# 2. 自动生成 main.spec (使用 EOF 写入，确保缩进正确)
cat > main.spec <<EOF
# -*- mode: python ; coding: utf-8 -*-
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
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='main',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=True, name='main')
EOF

# 3. 执行打包
pyinstaller main.spec

rm -rf build/