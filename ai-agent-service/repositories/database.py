"""Repository 的统一数据库配置与连接提供者，集中隔离 SQLite/PostgreSQL 方言。"""

import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class DatabaseConfig:
    """读取运行时数据库提供方；默认 SQLite，PostgreSQL 必须显式配置 DSN。"""

    provider: str
    sqlite_path: Path | None = None
    postgres_dsn: str | None = None

    @classmethod
    def from_env(cls, sqlite_path: str | Path | None = None) -> "DatabaseConfig":
        provider = os.getenv("DB_PROVIDER", "sqlite").lower()
        if provider not in {"sqlite", "postgres"}:
            raise ValueError("DB_PROVIDER 只能是 sqlite 或 postgres")
        if provider == "sqlite":
            return cls(provider="sqlite", sqlite_path=Path(sqlite_path) if sqlite_path else None)
        dsn = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
        if not dsn:
            raise ValueError("DB_PROVIDER=postgres 时必须配置 DATABASE_URL")
        return cls(provider="postgres", postgres_dsn=dsn)


class DatabaseAdapter:
    """向 Repository 提供统一连接、占位符转换和 PostgreSQL 行字典访问。"""

    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config

    @property
    def is_postgres(self) -> bool:
        return self.config.provider == "postgres"

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """SQLite 使用短连接；PostgreSQL 优先使用 psycopg_pool，未安装时清晰失败。"""
        if not self.is_postgres:
            if not self.config.sqlite_path:
                raise ValueError("SQLite 连接缺少数据库路径")
            conn = sqlite3.connect(self.config.sqlite_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()
            return
        try:
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL Repository 需要安装 psycopg_pool") from exc
        pool = ConnectionPool(self.config.postgres_dsn, kwargs={"row_factory": dict_row}, min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")), max_size=int(os.getenv("DB_POOL_MAX_SIZE", "10")), open=True)
        with pool.connection() as conn:
            try:
                yield _PostgresConnection(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                pool.close()


class _PostgresConnection:
    """兼容现有 Repository 的 execute/fetch 调用，把 SQLite ? 占位符集中转换为 %s。"""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        sql = re.sub(r"\?", "%s", sql)
        cursor = self.connection.cursor()
        cursor.execute(sql, params)
        return cursor
