# 数据库 Schema 版本与迁移

本文档说明 Workbuddy2API 的 SQLite 数据库版本化迁移机制，以及**后续新增表结构时应如何操作**，确保新旧库兼容、数据不丢失。

## 版本机制

- 使用 SQLite 原生 `PRAGMA user_version` 持久化 schema 版本号。
- 当前版本常量：`workbuddy_one/db.py` 中的 `SCHEMA_VERSION`。
- 启动时 `Database.__init__` 自动执行迁移：从当前 `user_version` **逐级升级**到 `SCHEMA_VERSION`，全程幂等、保留数据。

## 版本历史

| 版本 | 变更内容 |
|---|---|
| v1 | 初始结构：`accounts` / `usage_logs` / `settings` / `apps` 基础表 |
| v2 | `accounts` 补 `last_checkin_date`；`usage_logs` 补 `input_content` / `output_content` / `reasoning_content` / `credits` / `app_name` |
| v3 | `apps` 补 `key_enc`（应用 Key 加密存储） |

## 旧库自动合并

除版本升级外，启动时还会**自动发现并合并旧版数据库**（顶层 `workbuddy.db` / `data-top-level/workbuddy.db`）：
- 仅当当前库**无数据**时才合并（避免覆盖）
- 合并 `accounts` / `usage_logs` / `settings` 三张表
- `uid` 冲突时忽略旧行（`INSERT OR IGNORE`）

## 如何新增一个表结构版本（后续演进规范）

每当你需要修改表结构（加列 / 建表 / 改类型），按以下步骤操作：

1. **`SCHEMA_VERSION` +1**（例如从 3 → 4）
2. **新增一个迁移方法**，如：

```python
def _migration_v4(self):
    """v4：新增 xxx 结构。"""
    self._add_column("accounts", "new_col", "TEXT")
    self._executescript_migrate("""
        CREATE TABLE IF NOT EXISTS new_table (...);
    """)
```

   > 迁移方法**必须幂等**：先查列/表是否存在再操作（`_add_column` 已内置判断）。

3. **在 `_MIGRATIONS` 追加条目**：

```python
_MIGRATIONS = [
    (1, _migration_v1),
    (2, _migration_v2),
    (3, _migration_v3),
    (4, _migration_v4),   # 新增
]
```

4. **更新本文档的「版本历史」表**，记录 v4 变更内容。

## 迁移安全说明

- 迁移在启动时、同事务内执行；任一迁移失败会抛出异常阻止启动（避免半迁移状态）。
- 新增列用 `ALTER TABLE ... ADD COLUMN`（仅支持添加，不支持删除/改名/改类型）。
- 若需要**删除列 / 改名 / 改类型**：SQLite 需**重建表**（建新表 → 拷数据 → 改名 → 重建索引），请在迁移方法内自行实现，并保证幂等。
- 新库（`user_version=0`）也会走完整迁移链，但所有操作幂等，无副作用。

## 查看当前数据库版本

```bash
sqlite3 data/workbuddy.db "PRAGMA user_version;"
```
