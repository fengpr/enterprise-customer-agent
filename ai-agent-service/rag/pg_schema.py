import argparse
from typing import Any

from rag.pg_config import RagConfigError, load_embedding_config, load_pgvector_config


REQUIRED_TABLES = {"rag_documents", "rag_chunks", "rag_embeddings", "rag_retrieval_logs"}


def main() -> None:
    """命令行入口：初始化 pgvector schema 或单独创建索引。"""
    parser = argparse.ArgumentParser(description="Manage Postgres + pgvector schema for RAG.")
    parser.add_argument("command", choices=["init", "create-indexes", "check"])
    parser.add_argument("--skip-vector-index", action="store_true", help="大批量导入前跳过 HNSW 索引创建")
    args = parser.parse_args()

    if args.command == "init":
        init_schema(skip_vector_index=args.skip_vector_index)
        return
    if args.command == "create-indexes":
        create_indexes(include_vector=True)
        return
    check_schema()
    print("RAG pgvector schema is ready.")


def init_schema(*, skip_vector_index: bool = False) -> None:
    """创建扩展、表和索引；该函数只由 migration 命令调用，服务启动不调用。"""
    config = load_pgvector_config(require_api_key=False)
    embedding = config.embedding
    statements = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        _documents_table_sql(),
        _chunks_table_sql(),
        _embeddings_table_sql(embedding.dimension),
        _retrieval_logs_table_sql(),
    ]
    with _connect(config.database_url) as conn:
        with conn.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            for statement in _index_sql(include_vector=not skip_vector_index):
                cursor.execute(statement)
        conn.commit()


def create_indexes(*, include_vector: bool = True) -> None:
    """为已有表补建索引，支持大批量导入后再创建 HNSW。"""
    config = load_pgvector_config(require_api_key=False)
    with _connect(config.database_url) as conn:
        with conn.cursor() as cursor:
            for statement in _index_sql(include_vector=include_vector):
                cursor.execute(statement)
        conn.commit()


def check_schema(database_url: str | None = None) -> None:
    """只检查 schema 是否存在，不执行任何 DDL，供服务启动阶段调用。"""
    if database_url is None:
        config = load_pgvector_config(require_api_key=False)
        database_url = config.database_url
    with _connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            if cursor.fetchone() is None:
                raise RagConfigError("pgvector extension is not installed; run `python -m rag.pg_schema init`")
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = ANY(%s)
                """,
                (list(REQUIRED_TABLES),),
            )
            found = {row[0] for row in cursor.fetchall()}
            missing = REQUIRED_TABLES - found
            if missing:
                raise RagConfigError(f"RAG schema is missing tables: {sorted(missing)}")


def schema_sql(*, dimension: int | None = None, include_vector_index: bool = True) -> str:
    """返回完整 schema SQL，供测试和人工审阅使用。"""
    embedding = load_embedding_config(require_api_key=False)
    dim = dimension or embedding.dimension
    statements = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        _documents_table_sql(),
        _chunks_table_sql(),
        _embeddings_table_sql(dim),
        _retrieval_logs_table_sql(),
        *_index_sql(include_vector=include_vector_index),
    ]
    return ";\n\n".join(statement.strip().rstrip(";") for statement in statements) + ";"


def _documents_table_sql() -> str:
    """rag_documents 保存文档级元数据和源文件信息。"""
    return """
    CREATE TABLE IF NOT EXISTS rag_documents (
        id BIGSERIAL PRIMARY KEY,
        doc_name TEXT NOT NULL,
        version TEXT NOT NULL,
        collection TEXT NOT NULL,
        business_scope TEXT NOT NULL,
        source_path TEXT,
        source_type TEXT NOT NULL DEFAULT 'official_policy',
        status TEXT NOT NULL DEFAULT 'PUBLISHED',
        risk_level TEXT NOT NULL DEFAULT 'low',
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        content_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (doc_name, version, collection, business_scope)
    )
    """


def _chunks_table_sql() -> str:
    """rag_chunks 保存清洗拆分后的知识片段，向量版本放在 rag_embeddings。"""
    return """
    CREATE TABLE IF NOT EXISTS rag_chunks (
        id BIGSERIAL PRIMARY KEY,
        document_id BIGINT NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        paragraph TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        collection TEXT NOT NULL,
        business_scope TEXT NOT NULL,
        heading_path TEXT[] NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'PUBLISHED',
        risk_level TEXT NOT NULL DEFAULT 'low',
        answerable_intents TEXT[] NOT NULL DEFAULT '{}',
        source_type TEXT NOT NULL DEFAULT 'official_policy',
        effective_time TIMESTAMPTZ,
        expire_time TIMESTAMPTZ,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (document_id, chunk_index, content_hash)
    )
    """


def _embeddings_table_sql(dimension: int) -> str:
    """rag_embeddings 支持同一个 chunk 保存多个 embedding_version。"""
    return f"""
    CREATE TABLE IF NOT EXISTS rag_embeddings (
        id BIGSERIAL PRIMARY KEY,
        chunk_id BIGINT NOT NULL REFERENCES rag_chunks(id) ON DELETE CASCADE,
        embedding_provider TEXT NOT NULL,
        embedding_model TEXT NOT NULL,
        embedding_dimension INTEGER NOT NULL,
        embedding_distance TEXT NOT NULL,
        embedding_version TEXT NOT NULL,
        embedding vector({dimension}) NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (chunk_id, embedding_version)
    )
    """


def _retrieval_logs_table_sql() -> str:
    """rag_retrieval_logs 记录检索输入、过滤器、分数和 no-hit 判断。"""
    return """
    CREATE TABLE IF NOT EXISTS rag_retrieval_logs (
        id BIGSERIAL PRIMARY KEY,
        query TEXT NOT NULL,
        filters JSONB NOT NULL DEFAULT '{}'::jsonb,
        top_k INTEGER NOT NULL,
        scores JSONB NOT NULL DEFAULT '[]'::jsonb,
        selected_chunk_ids BIGINT[] NOT NULL DEFAULT '{}',
        latency_ms INTEGER NOT NULL,
        no_hit BOOLEAN NOT NULL DEFAULT false,
        embedding_version TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """


def _index_sql(*, include_vector: bool) -> list[str]:
    """生成普通索引、GIN 索引和可选 HNSW cosine 索引。"""
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_rag_documents_collection ON rag_documents(collection)",
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_collection_scope ON rag_chunks(collection, business_scope)",
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_status_risk ON rag_chunks(status, risk_level)",
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_effective_expire ON rag_chunks(effective_time, expire_time)",
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_answerable_intents ON rag_chunks USING GIN(answerable_intents)",
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_metadata ON rag_chunks USING GIN(metadata)",
        "CREATE INDEX IF NOT EXISTS idx_rag_embeddings_version ON rag_embeddings(embedding_version)",
        "CREATE INDEX IF NOT EXISTS idx_rag_retrieval_logs_created_at ON rag_retrieval_logs(created_at)",
    ]
    if include_vector:
        statements.append(
            "CREATE INDEX IF NOT EXISTS idx_rag_embeddings_hnsw_cosine "
            "ON rag_embeddings USING hnsw (embedding vector_cosine_ops)"
        )
    return statements


def _connect(database_url: str) -> Any:
    """延迟导入 psycopg，避免未安装依赖时影响 memory 后端测试。"""
    try:
        import psycopg
    except ImportError as exc:
        raise RagConfigError("psycopg is required for pgvector backend") from exc
    return psycopg.connect(database_url)


if __name__ == "__main__":
    main()
