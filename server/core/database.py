import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# 1. 智能识别运行环境根目录
if getattr(sys, 'frozen', False):
    # 🧊 打包环境 (PyInstaller)
    # sys.executable -> dist/main/main (Mac/Linux) 或 dist/main/main.exe (Win)
    # dirname(sys.executable) -> dist/main (程序文件夹)
    # dirname(dirname(...)) -> dist (程序文件夹的上一级)
    # 这样 data 目录会生成在 dist/data，与 main 文件夹同级，更新程序不会丢失数据
    BASE_DIR = os.path.dirname(os.path.dirname(sys.executable))
else:
    # 🐍 开发环境
    # 获取当前文件 (database.py) 的绝对路径 -> .../server/core/database.py
    # 回退 3 层找到项目根目录 -> .../MiniOrangeServer
    CURRENT_FILE_PATH = os.path.abspath(__file__)
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(CURRENT_FILE_PATH)))

# 2. 拼接 data 目录路径
DATA_DIR = os.path.join(BASE_DIR, "data")

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