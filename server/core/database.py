import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# 1. 获取当前文件 (database.py) 的绝对路径
# 例如: D:/Project/server/app/core/database.py
CURRENT_FILE_PATH = os.path.abspath(__file__)

# 2. 反向推导 server 根目录
# 第1层dirname -> app/core
# 第2层dirname -> app
# 第3层dirname -> server
SERVER_DIR = os.path.dirname(os.path.dirname(CURRENT_FILE_PATH))

# 3. 拼接 data 目录路径 (D:/Project/server/data)
DATA_DIR = os.path.join(SERVER_DIR, "data")

# 4. 关键：如果没有 data 目录，自动创建它
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