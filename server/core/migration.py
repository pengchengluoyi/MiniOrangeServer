# !/usr/bin/env python
# -*-coding:utf-8 -*-
import os
import uuid
import sqlite3
from script.log import SLog
from server.core.database import APP_DATA_DIR

TAG = "Migration"


def run_auto_migration():
    """
    自动检测并修复数据库表结构 (在服务启动时调用)
    """
    data_dir = os.path.join(APP_DATA_DIR, "data")
    if not os.path.exists(data_dir):
        return

    # 扫描目录下所有的 .db 文件 (通常是 autobots.db 或 miniorange.db)
    for filename in os.listdir(data_dir):
        if filename.endswith(".db"):
            db_path = os.path.join(data_dir, filename)
            try:
                _check_and_migrate(db_path)
            except Exception as e:
                SLog.e(TAG, f"Migration failed for {filename}: {e}")


def _check_and_migrate(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # --- 定义需要检查的表和字段 ---
        # 格式: '表名': [('字段名', '类型', '默认值')]
        # 如果你以后加了新字段，只需要在这里追加即可
        schema_changes = {
            'app_graph': [  # 注意：SQLAlchemy 模型定义的表名
                ('app_id', 'TEXT', None),
                ('created_at', 'DATETIME', None),
                ('uid', 'TEXT', None)
            ],
            'app_nodes': [
                ('workflow_id', 'TEXT', None)
            ],
            # 兼容旧表名 (防止表名修改导致旧数据无法迁移)
            'projects': [
                ('uid', 'TEXT', None)
            ],
            'apps': [
                ('uid', 'TEXT', None)
            ],
            'tasks': [
                ('uid', 'TEXT', None),
                ('result_summary', 'JSON', None)
            ],
            'm_device': [
                ('role', 'TEXT', 'node'),
                ('password', 'TEXT', None)
            ],
            'scheduled_tasks': [
                ('app_id', 'TEXT', None),
                ('skip_nodes', 'TEXT', None)
            ]
        }

        for table, columns in schema_changes.items():
            # 1. 检查表是否存在
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            if not cursor.fetchone():
                continue

            # 2. 获取现有列
            cursor.execute(f"PRAGMA table_info({table})")
            existing_cols = {row[1] for row in cursor.fetchall()}

            # 3. 检查并添加缺失列
            for col_name, col_type, _ in columns:
                if col_name not in existing_cols:
                    SLog.i(TAG,
                           f"🛠️ Migrating: Adding column '{col_name}' to table '{table}' in {os.path.basename(db_path)}")
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")

                    # 4. 特殊处理：如果是 uid 字段，需要为每一行生成唯一的 UUID
                    if col_name == 'uid':
                        cursor.execute(f"SELECT rowid FROM {table} WHERE {col_name} IS NULL")
                        rows = cursor.fetchall()
                        if rows:
                            SLog.i(TAG, f"   -> Backfilling UUIDs for {len(rows)} rows in {table}...")
                            for row in rows:
                                new_uid = str(uuid.uuid4())
                                cursor.execute(f"UPDATE {table} SET {col_name} = ? WHERE rowid = ?", (new_uid, row[0]))

                    # 5. 处理其他有默认值的字段 (如 app_id)
                    elif col_name == 'app_id':
                        cursor.execute(f"UPDATE {table} SET {col_name} = 'default_app' WHERE {col_name} IS NULL")
                    
                    elif col_name == 'role':
                        cursor.execute(f"UPDATE {table} SET {col_name} = 'node' WHERE {col_name} IS NULL")

        conn.commit()
    finally:
        conn.close()
