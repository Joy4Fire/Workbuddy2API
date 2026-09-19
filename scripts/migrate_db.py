"""数据库迁移脚本（独立运行版）。

正常情况下迁移在服务启动时自动执行（Database.__init__），无需手动操作。
本脚本用于：
  - 升级前**预迁移**（先迁库再起服务，失败可提前发现）
  - 检查当前库版本 / 触发迁移前自动备份
  - 无人值守部署流程中的显式迁移步骤

用法（在项目根目录）：
    python scripts/migrate_db.py                # 迁移默认库 data/workbuddy.db
    python scripts/migrate_db.py --db 路径      # 迁移指定库
    python scripts/migrate_db.py --check        # 只查看版本，不做迁移

行为：
  - 版本低于最新 → 先自动备份到 data/backups/（保留 5 份）再逐级迁移，数据保留
  - 已是最新版本 → 原样退出（幂等，无副作用）
  - 全新空库   → 建表到最新版本（不产生迁移备份）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 使脚本可直接运行：把项目根加入 import 路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workbuddy_one.db import SCHEMA_VERSION, Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Workbuddy2API 数据库版本检查 / 迁移")
    parser.add_argument("--db", default="data/workbuddy.db", help="数据库文件路径（默认 data/workbuddy.db）")
    parser.add_argument("--check", action="store_true", help="只查看版本，不执行迁移")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[i] 数据库不存在：{db_path}（服务首次启动时会自动创建）")
        return 0

    # 只读连接查看当前版本
    raw = sqlite3.connect(str(db_path))
    current = raw.execute("PRAGMA user_version").fetchone()[0]
    tables = raw.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    raw.close()
    print(f"[i] 数据库：{db_path}")
    print(f"[i] 当前 schema 版本：v{current}，目标版本：v{SCHEMA_VERSION}，共 {tables} 张表")

    if args.check:
        print("[i] --check 模式：未做任何修改")
        return 0

    if current >= SCHEMA_VERSION:
        print("[✓] 已是最新版本，无需迁移")
        return 0

    print(f"[i] 开始迁移 v{current} → v{SCHEMA_VERSION}（迁移前自动备份到 data/backups/）...")
    db = Database(str(db_path))  # __init__ 内：备份 → 建表 → 逐级迁移 → 索引
    try:
        final = db._conn.execute("PRAGMA user_version").fetchone()[0]
        if final != SCHEMA_VERSION:
            print(f"[✗] 迁移后版本异常：v{final}（期望 v{SCHEMA_VERSION}），请检查 db.py 的 _MIGRATIONS")
            return 1
        accounts = db._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        usage = db._conn.execute("SELECT COUNT(*) FROM usage_logs").fetchone()[0]
        print(f"[✓] 迁移完成：v{current} → v{final}")
        print(f"[i] 数据保留：accounts {accounts} 条，usage_logs {usage} 条")
        backups = sorted((db_path.parent / "backups").glob("*.bak")) if (db_path.parent / "backups").exists() else []
        if backups:
            print(f"[i] 最近备份：{backups[-1].name}")
        return 0
    finally:
        db._conn.close()


if __name__ == "__main__":
    sys.exit(main())
