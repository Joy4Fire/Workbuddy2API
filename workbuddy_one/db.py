"""SQLite 访问层：初始化表结构，提供读写。

Phase 1 先建 accounts（认证账号）与 usage_logs（使用记录）两张表。
使用标准库 sqlite3 + 线程锁（单用户低并发足够，后续可换 aiosqlite）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

# 当前数据库 schema 版本（用 SQLite PRAGMA user_version 持久化）。
# 每次对表结构做不兼容/增量修改时 +1，并在 _migrate 里追加对应迁移步骤。
SCHEMA_VERSION = 3


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        # 发现并合并旧版数据库（如顶层 workbuddy.db / data-top-level），幂等
        self._migrate_legacy_dbs()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT UNIQUE,
                    nickname TEXT,
                    enterprise_id TEXT,
                    domain TEXT,
                    auth_json TEXT,            -- 完整 auth 内容
                    enabled INTEGER DEFAULT 1,
                    priority INTEGER DEFAULT 0,
                    credits_remaining REAL,
                    credits_total REAL,
                    credits_expire_at TEXT,
                    last_used_at REAL,
                    last_checkin_date TEXT,    -- 最近一次签到日期 YYYY-MM-DD
                    failure_count INTEGER DEFAULT 0,
                    cooldown_until REAL DEFAULT 0,
                    created_at REAL,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL,
                    model TEXT,
                    protocol TEXT,             -- chat / responses / anthropic
                    account_uid TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    latency_ms REAL,
                    status TEXT,               -- ok / error
                    error TEXT,
                    input_content TEXT,        -- 请求输入（用户消息/历史）
                    output_content TEXT,       -- 模型最终输出文本
                    reasoning_content TEXT,    -- 思考链 / COT 内容
                    credits REAL               -- 本次请求消耗的积分
                );
                CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_logs(ts);
                CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_logs(model);
                CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_logs(account_uid);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS apps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    key_hash TEXT UNIQUE,      -- 应用 Key 的 sha256（不存明文）
                    key_prefix TEXT,           -- 明文 Key 的前缀（展示用，如 sk-a1b2…）
                    note TEXT,
                    enabled INTEGER DEFAULT 1,
                    created_at REAL
                );
                """
            )
            # 版本化增量迁移：把旧 schema 库升级到当前版本（幂等，保留数据）
            self._migrate()
            self._conn.commit()

    # ---------------- schema 版本化迁移 ----------------

    # 当前各表的完整列定义（列名 → DDL 类型）。迁移时兜底补齐缺失列，
    # 保证无论旧库多老，最终都对齐当前 schema。
    _FULL_COLUMNS = {
        "accounts": [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("uid", "TEXT"),
            ("nickname", "TEXT"), ("enterprise_id", "TEXT"), ("domain", "TEXT"),
            ("auth_json", "TEXT"), ("enabled", "INTEGER DEFAULT 1"),
            ("priority", "INTEGER DEFAULT 0"), ("credits_remaining", "REAL"),
            ("credits_total", "REAL"), ("credits_expire_at", "TEXT"),
            ("last_used_at", "REAL"), ("last_checkin_date", "TEXT"),
            ("failure_count", "INTEGER DEFAULT 0"), ("cooldown_until", "REAL DEFAULT 0"),
            ("created_at", "REAL"), ("updated_at", "REAL"),
        ],
        "usage_logs": [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("ts", "REAL"),
            ("model", "TEXT"), ("protocol", "TEXT"), ("account_uid", "TEXT"),
            ("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"), ("latency_ms", "REAL"),
            ("status", "TEXT"), ("error", "TEXT"),
            ("input_content", "TEXT"), ("output_content", "TEXT"),
            ("reasoning_content", "TEXT"), ("credits", "REAL"), ("app_name", "TEXT"),
        ],
        "apps": [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("name", "TEXT"),
            ("key_hash", "TEXT"), ("key_prefix", "TEXT"), ("note", "TEXT"),
            ("enabled", "INTEGER DEFAULT 1"), ("created_at", "REAL"), ("key_enc", "TEXT"),
        ],
    }

    # ---------------- 版本化迁移框架 ----------------
    #
    # 每次对表结构做不兼容/增量修改时：
    #   1) SCHEMA_VERSION +1
    #   2) 在 _MIGRATIONS 追加一个 (版本号, 迁移函数)
    #   3) 迁移函数必须幂等（先查列/表是否存在再动手）
    # 启动时从 PRAGMA user_version 逐级执行到 SCHEMA_VERSION，
    # 旧库自动升级、数据保留；新库 user_version=0 也走完整迁移链（幂等无副作用）。

    def _migration_v1(self):
        """v1：初始结构（accounts/usage_logs/settings/apps 已由 _init_schema 创建）。"""
        # 基础表由 _init_schema 的 CREATE TABLE IF NOT EXISTS 保证存在，此处无额外动作

    def _migration_v2(self):
        """v2：accounts 补 last_checkin_date；usage_logs 补内容列 / credits / app_name。"""
        self._add_column("accounts", "last_checkin_date", "TEXT")
        for col in ("input_content", "output_content", "reasoning_content"):
            self._add_column("usage_logs", col, "TEXT")
        self._add_column("usage_logs", "credits", "REAL")
        self._add_column("usage_logs", "app_name", "TEXT")

    def _migration_v3(self):
        """v3：apps 补 key_enc（加密明文 Key，配套应用鉴权改造）。"""
        self._add_column("apps", "key_enc", "TEXT")

    # 迁移注册表：每个条目 = (目标版本号, 迁移函数)。按版本号升序。
    # 后续新增结构 → 在此追加新条目，并在 SCHEMA_VERSION 处 +1。
    _MIGRATIONS = [
        (1, _migration_v1),
        (2, _migration_v2),
        (3, _migration_v3),
    ]

    # 兜底列定义：对「未知极老库」最后统一补齐当前全部列。
    # 已按版本迁移的库到这里通常无缺失列（幂等无副作用）。
    _FULL_COLUMNS = {
        "accounts": [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("uid", "TEXT"),
            ("nickname", "TEXT"), ("enterprise_id", "TEXT"), ("domain", "TEXT"),
            ("auth_json", "TEXT"), ("enabled", "INTEGER DEFAULT 1"),
            ("priority", "INTEGER DEFAULT 0"), ("credits_remaining", "REAL"),
            ("credits_total", "REAL"), ("credits_expire_at", "TEXT"),
            ("last_used_at", "REAL"), ("last_checkin_date", "TEXT"),
            ("failure_count", "INTEGER DEFAULT 0"), ("cooldown_until", "REAL DEFAULT 0"),
            ("created_at", "REAL"), ("updated_at", "REAL"),
        ],
        "usage_logs": [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("ts", "REAL"),
            ("model", "TEXT"), ("protocol", "TEXT"), ("account_uid", "TEXT"),
            ("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"), ("latency_ms", "REAL"),
            ("status", "TEXT"), ("error", "TEXT"),
            ("input_content", "TEXT"), ("output_content", "TEXT"),
            ("reasoning_content", "TEXT"), ("credits", "REAL"), ("app_name", "TEXT"),
        ],
        "apps": [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"), ("name", "TEXT"),
            ("key_hash", "TEXT"), ("key_prefix", "TEXT"), ("note", "TEXT"),
            ("enabled", "INTEGER DEFAULT 1"), ("created_at", "REAL"), ("key_enc", "TEXT"),
        ],
    }

    def _user_version(self) -> int:
        try:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]
        except (sqlite3.Error, IndexError, TypeError):
            return 0

    def _table_columns(self, table: str) -> list[str]:
        """返回表的所有列名（表不存在返回空列表）。"""
        try:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            return []
        return [r[1] for r in rows]

    def _add_column(self, table: str, col: str, ddl: str):
        """列不存在则添加。"""
        if col not in self._table_columns(table):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def _executescript_migrate(self, script: str):
        """在迁移中执行多语句 SQL（建表/索引等）。调用方保证幂等。"""
        self._conn.executescript(script)

    def _migrate(self):
        """按版本号把数据库升级到 SCHEMA_VERSION（版本化增量迁移 + 兜底补列）。

        从 PRAGMA user_version 逐级执行 _MIGRATIONS 中更高版本的迁移，每步幂等；
        最后兜底补齐缺失列，确保极老库也对齐当前结构。数据全程保留。
        """
        ver = self._user_version()
        # 逐级执行迁移到当前版本
        for target, fn in self._MIGRATIONS:
            if ver < target:
                fn(self)
                ver = target
        # 兜底补齐所有当前列（对未知极老库的最终保险）
        for table, cols in self._FULL_COLUMNS.items():
            for col, ddl in cols:
                if ddl.startswith("INTEGER PRIMARY KEY"):
                    continue
                self._add_column(table, col, ddl)
        # 写入最新版本号
        self._conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    # ---------------- 旧库发现与数据合并 ----------------

    def _migrate_legacy_dbs(self):
        """检测旧版数据库文件并合并数据到当前库（幂等，仅当前库无对应数据时迁移）。

        旧版 workbuddy-one 曾把数据库放在包根目录（workbuddy.db）或 data-top-level/。
        若当前库是全新空库（无账号/设置），且发现旧库，则把旧库的
        accounts / usage_logs / settings 数据复制进来，避免用户"数据丢失"。
        """
        from .config import PACKAGE_ROOT

        # 当前库已有数据则跳过（避免覆盖）
        try:
            has_data = self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] > 0 or \
                       bool(self._conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone())
        except sqlite3.Error:
            has_data = True
        if has_data:
            return

        candidates = [
            PACKAGE_ROOT / "workbuddy.db",                 # 顶层旧版
            PACKAGE_ROOT / "data-top-level" / "workbuddy.db",  # 归档旧版
            self.path.parent.parent / "workbuddy.db",      # 项目根
        ]
        for legacy in candidates:
            if not legacy.is_file():
                continue
            try:
                migrated = self._merge_legacy_db(str(legacy))
            except sqlite3.Error:
                continue
            if migrated:
                break

    def _merge_legacy_db(self, legacy_path: str):
        """把旧库的 accounts/settings/usage_logs 合并进当前库。返回是否迁移了数据。"""
        src = sqlite3.connect(legacy_path)
        try:
            src.row_factory = sqlite3.Row
            # 迁移 settings（非默认键，保留旧配置）
            for r in src.execute("SELECT key, value FROM settings"):
                self._conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (r["key"], r["value"]))
            # 迁移 accounts（uid 唯一，冲突忽略）
            acct_cols = self._table_columns("accounts")
            for r in src.execute("SELECT * FROM accounts"):
                d = dict(r)
                keys = [k for k in d if k in acct_cols and d[k] is not None]
                cols = ", ".join(keys)
                ph = ", ".join("?" * len(keys))
                self._conn.execute(
                    f"INSERT OR IGNORE INTO accounts ({cols}) VALUES ({ph})",
                    [d[k] for k in keys])
            # 迁移 usage_logs
            ug_cols = self._table_columns("usage_logs")
            for r in src.execute("SELECT * FROM usage_logs"):
                d = dict(r)
                keys = [k for k in d if k in ug_cols and d[k] is not None]
                cols = ", ".join(keys)
                ph = ", ".join("?" * len(keys))
                self._conn.execute(
                    f"INSERT OR IGNORE INTO usage_logs ({cols}) VALUES ({ph})",
                    [d[k] for k in keys])
            self._conn.commit()
            return True
        finally:
            src.close()

    # ---- accounts ----
    def upsert_account(self, auth: dict, account: dict | None = None):
        """写入一个 auth 文件内容。account 缺省时从 auth.account 解析。"""
        uid = account.get("uid") if account else (auth.get("account") or {}).get("uid")
        nickname = account.get("nickname") if account else (auth.get("account") or {}).get("nickname")
        ent = None
        if account:
            ent = account.get("enterpriseId")
        else:
            ent = (auth.get("account") or {}).get("enterpriseId")
        auth_data = auth.get("auth") if isinstance(auth.get("auth"), dict) else auth
        domain = auth_data.get("domain", "")
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO accounts
                   (uid, nickname, enterprise_id, domain, auth_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(uid) DO UPDATE SET
                     nickname=excluded.nickname,
                     enterprise_id=excluded.enterprise_id,
                     domain=excluded.domain,
                     auth_json=excluded.auth_json,
                     updated_at=excluded.updated_at
                """,
                (uid, nickname, ent, domain, json.dumps(auth, ensure_ascii=False), now, now),
            )
            self._conn.commit()
        return uid

    def list_accounts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY priority DESC, id ASC").fetchall()
        return [dict(r) for r in rows]

    def get_account(self, uid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row else None

    def delete_account(self, uid: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM accounts WHERE uid=?", (uid,))
            self._conn.commit()
        return cur.rowcount > 0

    def set_account_state(self, uid: str, **fields):
        allowed = {"enabled", "priority", "credits_remaining", "credits_total",
                   "credits_expire_at", "last_used_at", "failure_count", "cooldown_until"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(uid)
        with self._lock:
            self._conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE uid=?", vals)
            self._conn.commit()

    # ---- 签到状态 ----
    def set_checkin_date(self, uid: str, date: str):
        """记录某账号最近签到日期（YYYY-MM-DD）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE accounts SET last_checkin_date=?, updated_at=? WHERE uid=?",
                (date, time.time(), uid),
            )
            self._conn.commit()

    def checkin_dates(self) -> dict[str, str]:
        """返回 {uid: last_checkin_date}。"""
        with self._lock:
            rows = self._conn.execute("SELECT uid, last_checkin_date FROM accounts").fetchall()
        return {r["uid"]: r["last_checkin_date"] for r in rows if r["last_checkin_date"]}

    # ---- 设置 ----
    DEFAULT_SETTINGS = {
        "checkin_hours": "9,21",       # 每日自动签到小时点（逗号分隔）
        "credit_refresh_min": "30",    # 额度刷新间隔（分钟）
        "model_refresh_hour": "6",     # 每日自动刷新模型目录的小时（0-23）
        "aa_refresh_hour": "7",        # 每日自动刷新 AA 评测数据的小时（0-23）
        "keepalive_hour": "22",        # 每日 token 保活小时（0-23）
        "aa_api_key": "",              # Artificial Analysis API key（评测数据，空则不启用）
    }

    def get_settings(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        data = {r["key"]: r["value"] for r in rows}
        merged = dict(self.DEFAULT_SETTINGS)
        merged.update(data)
        return merged

    def save_settings(self, **kwargs):
        """保存设置。仅接受白名单内的 key。"""
        allowed = set(self.DEFAULT_SETTINGS.keys())
        with self._lock:
            for k, v in kwargs.items():
                if k in allowed:
                    self._conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, str(v)),
                    )
            self._conn.commit()

    # ---- usage_logs ----
    def log_usage(self, *, model, protocol, account_uid, input_tokens=0, output_tokens=0,
                  latency_ms=0.0, status="ok", error="",
                  input_content="", output_content="", reasoning_content="", credits=0.0,
                  app_name=""):
        total = int(input_tokens or 0) + int(output_tokens or 0)
        with self._lock:
            self._conn.execute(
                """INSERT INTO usage_logs
                   (ts, model, protocol, account_uid, input_tokens, output_tokens,
                    total_tokens, latency_ms, status, error,
                    input_content, output_content, reasoning_content, credits, app_name)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), model, protocol, account_uid, int(input_tokens or 0),
                 int(output_tokens or 0), total, latency_ms, status, error,
                 input_content or "", output_content or "", reasoning_content or "",
                 float(credits or 0), app_name or ""),
            )
            self._conn.commit()

    def cleanup_usage(self, retention_days: int):
        """删除超过保留期的使用记录。"""
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM usage_logs WHERE ts < ?", (cutoff,))
            self._conn.commit()

    def trim_usage_content(self, input_limit: int = 4000, output_limit: int = 8000,
                           reason_limit: int = 8000) -> int:
        """无损裁剪存量记录的超长 content（保留记录条目与 token/时长等元数据）。

        仅对超限字段追加截断说明，返回处理的记录条数。用于清理早期"全量历史重复入库"
        产生的超大文本（旧数据单条可达 30 万+ 字符）。
        """

        def _clip(v: str, limit: int) -> str:
            if not v or len(v) <= limit:
                return v
            return v[:limit] + f"\n…（已截断，原文 {len(v)} 字符）"

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, input_content, output_content, reasoning_content FROM usage_logs"
            ).fetchall()
            changed = 0
            for r in rows:
                ic = _clip(r["input_content"] or "", input_limit)
                oc = _clip(r["output_content"] or "", output_limit)
                rc = _clip(r["reasoning_content"] or "", reason_limit)
                if ic != r["input_content"] or oc != r["output_content"] or rc != r["reasoning_content"]:
                    self._conn.execute(
                        "UPDATE usage_logs SET input_content=?, output_content=?, reasoning_content=? WHERE id=?",
                        (ic, oc, rc, r["id"]),
                    )
                    changed += 1
            self._conn.commit()
        return changed

    def usage_content_stats(self) -> dict:
        """统计 content 体积：总大小、超限条数，供展示瘦身前后对比。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT input_content, output_content, reasoning_content FROM usage_logs"
            ).fetchall()
        total = 0
        over = 0
        for r in rows:
            s = len(r["input_content"] or "") + len(r["output_content"] or "") + len(r["reasoning_content"] or "")
            total += s
            if len(r["input_content"] or "") > 4000:
                over += 1
        return {"total_chars": total, "over_limit_rows": over, "rows": len(rows)}

    def usage_summary(self) -> dict:
        """聚合统计：总数、按协议、按模型、按日、今日 token。"""
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c, COALESCE(SUM(total_tokens),0) t FROM usage_logs").fetchone()
            today_start = int(time.time()) - int(time.time()) % 86400
            today = self._conn.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(total_tokens),0) t FROM usage_logs WHERE ts >= ?", (today_start,)
            ).fetchone()
            by_protocol = self._conn.execute(
                "SELECT protocol, COUNT(*) c, COALESCE(SUM(total_tokens),0) t FROM usage_logs GROUP BY protocol"
            ).fetchall()
            by_model = self._conn.execute(
                "SELECT model, COUNT(*) c, COALESCE(SUM(total_tokens),0) t FROM usage_logs GROUP BY model ORDER BY c DESC LIMIT 20"
            ).fetchall()
            by_app = self._conn.execute(
                "SELECT COALESCE(NULLIF(app_name,''),'(未命名)') app, COUNT(*) c, COALESCE(SUM(total_tokens),0) t FROM usage_logs GROUP BY app_name ORDER BY c DESC"
            ).fetchall()
        return {
            "total_requests": total["c"],
            "total_tokens": total["t"],
            "today_requests": today["c"],
            "today_tokens": today["t"],
            "by_protocol": [{"protocol": r["protocol"], "count": r["c"], "tokens": r["t"]} for r in by_protocol],
            "by_model": [{"model": r["model"], "count": r["c"], "tokens": r["t"]} for r in by_model],
            "by_app": [{"app": r["app"], "count": r["c"], "tokens": r["t"]} for r in by_app],
        }

    def usage_recent(self, limit: int = 50, protocol: str | None = None,
                     model: str | None = None, app_name: str | None = None,
                     status: str | None = None) -> list[dict]:
        """最近使用记录，支持按协议/模型/应用/状态筛选（空值表示该维度不筛选）。"""
        clauses: list[str] = []
        params: list = []
        if protocol:
            clauses.append("protocol = ?")
            params.append(protocol)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if app_name:
            # 空字符串表示"无应用名"（未命名/旧记录）
            clauses.append("COALESCE(app_name, '') = ?")
            params.append(app_name)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM usage_logs {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    def usage_filters(self) -> dict:
        """使用记录筛选项的可选值（协议/模型/应用/状态去重列表）。"""
        with self._lock:
            def col(field: str) -> list[str]:
                rows = self._conn.execute(
                    f"SELECT DISTINCT {field} FROM usage_logs "
                    f"WHERE {field} IS NOT NULL AND {field} != '' ORDER BY {field}"
                ).fetchall()
                return [str(r[0]) for r in rows]
            st = self._conn.execute(
                "SELECT DISTINCT status FROM usage_logs WHERE status IS NOT NULL AND status != ''"
            ).fetchall()
        return {
            "protocols": col("protocol"),
            "models": col("model"),
            "apps": col("app_name"),
            "statuses": [r[0] for r in st],
        }

    def usage_credit_stats(self) -> dict:
        """按模型累计积分与 token 统计，用于估算'每积分可换多少 token'。

        返回: {models: [{model, credits, tokens}], total_credits, total_tokens}
        只统计 credits > 0 的记录（有真实积分消耗、比例可信的模型）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT model, COALESCE(SUM(credits),0) c, COALESCE(SUM(total_tokens),0) t "
                "FROM usage_logs WHERE credits > 0 GROUP BY model ORDER BY c DESC"
            ).fetchall()
        models = [{"model": r["model"], "credits": float(r["c"] or 0), "tokens": float(r["t"] or 0)} for r in rows]
        return {
            "models": models,
            "total_credits": sum(m["credits"] for m in models),
            "total_tokens": sum(m["tokens"] for m in models),
        }

    def usage_timeseries(self, granularity: str = "hour", points: int = 24, model: str | None = None) -> list[dict]:
        """按时间分桶的调用趋势（横轴=时间，用于折线图）。

        granularity: 'hour' | 'day'
        points:      返回最近多少个桶
        model:       可选，仅统计某模型
        返回: [{bucket_ts, bucket, count, tokens}]，bucket 为易读标签。
        """
        bucket_sec = 3600 if granularity == "day" else 3600
        if granularity == "day":
            bucket_sec = 86400
        now = int(time.time())
        end = now - (now % bucket_sec)          # 对齐到桶边界
        start = end - (points - 1) * bucket_sec

        sql = ("SELECT (CAST(ts AS INTEGER) / ?) * ? AS bucket_ts, COUNT(*) c, COALESCE(SUM(total_tokens),0) t "
               "FROM usage_logs WHERE ts >= ? AND ts < ?")
        args: list = [bucket_sec, bucket_sec, start, end + bucket_sec]
        if model:
            sql += " AND model = ?"
            args.append(model)
        sql += " GROUP BY bucket_ts"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        by_bucket = {r["bucket_ts"]: (r["c"], r["t"]) for r in rows}

        # 补齐空桶
        import datetime as _dt
        out = []
        for i in range(points):
            bt = start + i * bucket_sec
            count, tokens = by_bucket.get(bt, (0, 0))
            label = _dt.datetime.fromtimestamp(bt).strftime("%m-%d %H:%M" if bucket_sec == 3600 else "%m-%d")
            out.append({"bucket_ts": bt, "bucket": label, "count": count, "tokens": tokens})
        return out

    # ---- apps（应用 API Key） ----
    def list_apps(self) -> list[dict]:
        """返回应用列表，附带各自累计用量（请求数/tokens/积分）。不返回加密密文。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, key_prefix, note, enabled, created_at FROM apps ORDER BY id ASC"
            ).fetchall()
            stats = self._conn.execute(
                "SELECT app_name, COUNT(*) c, COALESCE(SUM(total_tokens),0) t, COALESCE(SUM(credits),0) k "
                "FROM usage_logs GROUP BY app_name"
            ).fetchall()
        stat_map = {r["app_name"]: r for r in stats}
        out = []
        for r in rows:
            s = stat_map.get(r["name"])
            out.append({
                "id": r["id"],
                "name": r["name"],
                "key_prefix": r["key_prefix"],
                "note": r["note"] or "",
                "enabled": bool(r["enabled"]),
                "created_at": r["created_at"],
                "requests": s["c"] if s else 0,
                "tokens": s["t"] if s else 0,
                "credits": s["k"] if s else 0,
            })
        return out

    def create_app(self, *, name, key_hash, key_prefix, note="", key_enc=""):
        with self._lock:
            self._conn.execute(
                "INSERT INTO apps (name, key_hash, key_prefix, note, enabled, created_at, key_enc) VALUES (?,?,?,?,1,?,?)",
                (name, key_hash, key_prefix, note or "", time.time(), key_enc or ""),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT id FROM apps WHERE name = ?", (name,)).fetchone()
            return row["id"] if row else None

    def find_app_by_key(self, key_hash: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM apps WHERE key_hash = ? AND enabled = 1", (key_hash,)
            ).fetchone()
            return dict(r) if r else None

    def get_app_key_enc(self, app_id: int) -> str:
        """返回应用的加密 Key token（可能为空，历史应用未加密存储）。"""
        with self._lock:
            r = self._conn.execute("SELECT key_enc FROM apps WHERE id = ?", (app_id,)).fetchone()
            return r["key_enc"] if r and r["key_enc"] else ""

    def toggle_app(self, app_id: int) -> bool:
        with self._lock:
            r = self._conn.execute("SELECT enabled FROM apps WHERE id = ?", (app_id,)).fetchone()
            if not r:
                return False
            new = 0 if r["enabled"] else 1
            self._conn.execute("UPDATE apps SET enabled = ? WHERE id = ?", (new, app_id))
            self._conn.commit()
            return bool(new)

    def delete_app(self, app_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
            self._conn.commit()
            return cur.rowcount > 0
