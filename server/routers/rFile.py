# server/routers/rFile.py
import os
import uuid
import shutil
from fastapi import APIRouter, UploadFile, File

router = APIRouter(prefix="/file", tags=["File Upload"])


# 🔥 保持与 main.py 完全一致的路径逻辑
# rFile.py 在 server/routers/ 下，所以 main.py 在上一级 (server/) ??
# 不，根据你的截图，main.py 在 services/main/ 下。
# 为了绝对安全，我们不依赖相对路径回退，而是依赖 "当前工作目录" 或者 "绝对定位"
# 最稳妥的方式：直接去 sys.modules['__main__'] 的位置，或者假定 uploads 就在运行目录下。

# 这里假设 uploads 就在 services/main/uploads
# 如果 rFile.py 被 main.py 引用，我们可以让 main.py 传递配置，或者重复逻辑：
# rFile.py 的位置: services/main/server/routers/rFile.py (猜测结构)
# 简单粗暴点：
# 你的 main.py 在 services/main/main.py
# 你的 routers 在 services/main/server/routers/ ??
# 根据你的截图，uploads 和 main 在同一级。

# 我们用一种稍微笨但绝对稳的方法：
# 向上找，直到找到 uploads 目录，或者就在 main.py 旁创建
def get_upload_dir():
    # 方案：相对于 main.py (入口脚本)
    import __main__
    if hasattr(__main__, '__file__'):
        root = os.path.dirname(os.path.abspath(__main__.__file__))
    else:
        root = os.getcwd()
    path = os.path.join(root, "uploads")
    if not os.path.exists(path):
        os.makedirs(path)
    return path


UPLOAD_DIR = get_upload_dir()


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        file_ext = os.path.splitext(file.filename)[1] or ".jpg"
        unique_name = f"snap_{uuid.uuid4().hex[:8]}{file_ext}"
        file_path = os.path.join(UPLOAD_DIR, unique_name)

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        return {
            "code": 200,
            "msg": "success",
            "url": f"/static/{unique_name}"  # 前端拼接 host
        }
    except Exception as e:
        return {"code": 500, "msg": str(e)}