# server/app/core/log_database.py
import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# 1. 获取当前文件绝对路径
CURRENT_FILE_PATH = os.path.abspath(__file__)

# 2. 反向推导目录 (同你之前的逻辑)
SERVER_DIR = os.path.dirname(os.path.dirname(CURRENT_FILE_PATH))
DATA_DIR = os.path.join(SERVER_DIR, "data")

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# 3. 【关键修改】这里改成 logs.db，与 autobots.db 物理隔离
DB_PATH = os.path.join(DATA_DIR, "logs.db")

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# 4. 【关键修改】变量名加 Log 前缀，防止导入时搞混
log_engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False} # SQLite 必须加这个
)

# 独立的 Session 和 Base
LogSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=log_engine)
LogBase = declarative_base()

# 独立的依赖注入函数
def get_log_db():
    db = LogSessionLocal()
    try:
        yield db
    finally:
        db.close()