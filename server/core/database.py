import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# 1. 🔥 核心修复：使用系统用户数据目录 (User Data Directory)
# 解决软件更新后数据丢失的问题。数据将存储在:
# Windows: %APPDATA%\MiniOrangeServer (例如 C:\Users\xxx\AppData\Roaming\MiniOrangeServer)
# macOS: ~/Library/Application Support/MiniOrangeServer
def get_app_data_dir(app_name="MiniOrangeServer"):
    if sys.platform == 'win32':
        # 优先使用 APPDATA (Roaming)，其次 LOCALAPPDATA
        base = os.environ.get('APPDATA') or os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        path = os.path.join(base, app_name)
    elif sys.platform == 'darwin':
        path = os.path.expanduser(f"~/Library/Application Support/{app_name}")
    else:
        path = os.path.expanduser(f"~/.local/share/{app_name}")
    
    if not os.path.exists(path):
        os.makedirs(path)
    return path

APP_DATA_DIR = get_app_data_dir()
BASE_DIR = APP_DATA_DIR  # 兼容旧代码引用

# 2. 拼接 data 目录路径
DATA_DIR = os.path.join(APP_DATA_DIR, "data")

# 3. 关键：如果没有 data 目录，自动创建它
# (如果不创建，SQLite 无法写入文件会报错)
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# 5. 拼接数据库文件的完整绝对路径
# D:/Project/server/data/autobots.db
DB_PATH = os.path.join(DATA_DIR, "autobots.db")

# 6. 生成 SQLAlchemy 连接字符串
# 使用 3 个斜杠 /// 表示绝对路径
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# --- 以下代码保持不变 ---
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()