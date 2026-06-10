# server/app/core/log_database.py
import os
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from server.core.database import APP_DATA_DIR

# 1. 使用统一的用户数据目录，确保日志在更新后不丢失
DATA_DIR = os.path.join(APP_DATA_DIR, "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# 2. 数据库文件路径
DB_PATH = os.path.join(DATA_DIR, "logs.db")

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# 4. 【关键修改】变量名加 Log 前缀，防止导入时搞混
log_engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)


@event.listens_for(log_engine, "connect")
def _set_log_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

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