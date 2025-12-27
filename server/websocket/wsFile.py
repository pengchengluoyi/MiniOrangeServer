# --- 业务处理函数 ---
import os
import base64
from script.log import SLog
from server.core.database import APP_DATA_DIR

TAG = "rWebSocket"

# 确保上传目录存在 (复用 main.py 中的逻辑)
UPLOAD_DIR = os.path.join(APP_DATA_DIR, "uploads")
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)


async def handle_upload(data: dict):
    """
    处理文件上传
    协议: {"name": "文件名", "content": "base64字符串"}
    """
    file_name = data.get("name")
    content_b64 = data.get("content")

    if not file_name or not content_b64:
        return {"code": 400, "msg": "Missing 'name' or 'content' in data"}

    # 安全性：只取文件名，防止路径遍历攻击 (../../)
    file_name = os.path.basename(file_name)
    file_path = os.path.join(UPLOAD_DIR, file_name)

    try:
        # 处理 Base64 前缀 (例如: data:image/png;base64,...)
        if "," in content_b64:
            content_b64 = content_b64.split(",")[1]

        file_data = base64.b64decode(content_b64)

        # 写入文件
        with open(file_path, "wb") as f:
            f.write(file_data)

        SLog.i(TAG, f"File uploaded: {file_path}")
        return {
            "code": 200,
            "msg": "Upload success",
            "data": {
                "filename": file_name,
                "path": file_path,
                "url": f"/static/{file_name}"  # 对应 main.py 挂载的 static 路径
            }
        }
    except Exception as e:
        SLog.e(TAG, f"Upload failed: {e}")
        return {"code": 500, "msg": f"Upload error: {str(e)}"}


async def handle_get_file(data: dict):
    """
    获取文件内容
    协议: {"name": "文件名"}
    """
    file_name = data.get("name")
    if not file_name:
        return {"code": 400, "msg": "Missing 'name' in data"}

    file_name = os.path.basename(file_name)
    file_path = os.path.join(UPLOAD_DIR, file_name)

    if not os.path.exists(file_path):
        return {"code": 404, "msg": f"File '{file_name}' not found"}

    try:
        with open(file_path, "rb") as f:
            file_data = f.read()

        # 转为 Base64 字符串
        b64_str = base64.b64encode(file_data).decode('utf-8')

        return {
            "code": 200,
            "msg": "Success",
            "data": {
                "name": file_name,
                "content": b64_str
            }
        }
    except Exception as e:
        SLog.e(TAG, f"Read file failed: {e}")
        return {"code": 500, "msg": f"Read error: {str(e)}"}
