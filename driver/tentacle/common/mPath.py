import os
import platform

def get_adb_path():
    """动态获取集成的 ADB 路径"""
    # 判断是否在 PyInstaller 打包后的环境中
    # 开发环境下的相对路径
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    sys_folder = "mac" if platform.system() == "Darwin" else "win"
    adb_bin_dir = os.path.join(base_path, 'resource', 'platform-tools', sys_folder)

    # 根据系统补全文件名
    adb_exe = "adb.exe" if platform.system() == "Windows" else "adb"
    full_path = os.path.join(adb_bin_dir, adb_exe)
    # macOS 权限补丁：确保打包后的二进制文件有执行权限
    if platform.system() != "Windows" and os.path.exists(full_path):
        os.chmod(full_path, 0o755)

    return f'"{full_path}"'  # 加引号防止路径中有空格