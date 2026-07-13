# PostgreSQL 迁移说明

默认运行模式仍为 SQLite。迁移前先停止写入或切换维护窗口，备份全部 SQLite 文件，再执行 Python 与 Java 的 PostgreSQL 基线 DDL。

顺序：`customer → employee/product → order_info → logistics_info/logistics_trace → support_ticket/ticket_urge_log`；Python 侧为 `chat_session → chat_message → agent_call_log → evaluation_trace → evaluation_result/evaluation_job`。

Python 导出示例：`python scripts/migrate_sqlite_to_postgres.py data/customer_agent.db export.jsonl`。该命令先生成同目录 `.bak` 备份。使用 `python scripts/validate_migration.py export.jsonl` 记录行数和摘要；导入 PostgreSQL 后以同样的表行数与业务主键摘要比对。

执行 Python 导入前，先运行 `migrations/postgres/V1__agent_schema.sql`，再执行 `python scripts/import_postgres_export.py export.jsonl --dsn postgresql://...`。导入脚本使用单个 PostgreSQL 事务，出现 JSON、外键或唯一约束错误时不会提交部分结果。

回滚：导入必须在独立 PostgreSQL 事务/新库中执行，校验失败则回滚该事务或删除目标 schema；不要覆盖 SQLite `.bak` 文件。未完成校验前，应用继续指向 SQLite。`support_ticket.request_id` 和 `(customer_id, idempotency_key)` 有唯一约束，导入冲突必须人工确认，不能用覆盖写入掩盖重复建单。

## 运行时 Profile

- SQLite（默认开发）：Python 不设置 `DB_PROVIDER`，Java 使用 `application.yml`，执行 `mvn spring-boot:run`。
- PostgreSQL（验证环境）：Python 设置 `DB_PROVIDER=postgres` 与 `DATABASE_URL=postgresql://...`；Java 设置 `SPRING_PROFILES_ACTIVE=postgres`，Flyway 会执行 `db/migration/V1__business_postgres_schema.sql`。
- PostgreSQL profile 下 Java Service 不执行 SQLite 的 `CREATE TABLE`、`AUTOINCREMENT` 或 `PRAGMA` 兼容代码；结构只能由 Flyway 管理。
