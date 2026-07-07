import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rag.embeddings import OpenAICompatibleEmbeddingClient, vector_literal
from rag.evaluate import load_kb_chunks
from rag.pg_config import load_pgvector_config
from rag.text_processing import KnowledgeChunk


def main() -> None:
    """命令行入口：把 kb_sources 文档清洗、切块、向量化后写入 pgvector。"""
    parser = argparse.ArgumentParser(description="Ingest knowledge sources into Postgres + pgvector.")
    parser.add_argument("--kb-dir", default="data/kb_sources")
    parser.add_argument("--reset", action="store_true", help="清空 RAG 表后重新入库")
    args = parser.parse_args()

    summary = ingest_kb_sources(kb_dir=args.kb_dir, reset=args.reset)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def ingest_kb_sources(
    *,
    kb_dir: str,
    reset: bool = False,
    embedding_client: OpenAICompatibleEmbeddingClient | None = None,
) -> dict[str, Any]:
    """批量导入知识库文档；通过 content_hash 和 embedding_version 保证可重复运行。"""
    config = load_pgvector_config(require_api_key=True)
    chunks = load_kb_chunks(kb_dir)
    client = embedding_client or OpenAICompatibleEmbeddingClient(config.embedding)
    stats = {
        "documents": 0,
        "chunks": 0,
        "embedded": 0,
        "skipped_embeddings": 0,
        "embedding_version": config.embedding.version,
    }

    with _connect(config.database_url) as conn:
        if reset:
            _reset_tables(conn)
        pending: list[tuple[int, KnowledgeChunk, str]] = []
        for chunk in chunks:
            source_path = _find_source_path(kb_dir, chunk)
            document_hash = _hash_text(f"{chunk.doc_name}|{chunk.version}|{source_path or ''}")
            chunk_hash = content_hash(chunk)
            document_id = _upsert_document(conn, chunk, source_path=source_path, content_hash=document_hash)
            chunk_id = _upsert_chunk(conn, document_id=document_id, chunk=chunk, content_hash=chunk_hash)
            stats["chunks"] += 1
            if not _embedding_exists(conn, chunk_id=chunk_id, embedding_version=config.embedding.version):
                pending.append((chunk_id, chunk, chunk_hash))
            else:
                stats["skipped_embeddings"] += 1

        stats["documents"] = _count_documents(conn)
        for start in range(0, len(pending), config.embedding.batch_size):
            batch = pending[start : start + config.embedding.batch_size]
            vectors = client.embed_texts([item[1].paragraph for item in batch])
            for (chunk_id, _chunk, chunk_hash), vector in zip(batch, vectors, strict=True):
                _insert_embedding(conn, chunk_id=chunk_id, content_hash=chunk_hash, vector=vector)
                stats["embedded"] += 1
        conn.commit()
    return stats


def content_hash(chunk: KnowledgeChunk) -> str:
    """基于正文和核心 metadata 生成稳定 hash，用于幂等入库和重跑跳过。"""
    payload = {
        "paragraph": chunk.paragraph,
        "collection": chunk.collection,
        "business_scope": chunk.business_scope,
        "heading_path": chunk.heading_path,
        "risk_level": chunk.risk_level,
        "answerable_intents": chunk.answerable_intents,
        "status": chunk.status,
        "version": chunk.version,
    }
    return _hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _upsert_document(conn: Any, chunk: KnowledgeChunk, *, source_path: str | None, content_hash: str) -> int:
    """写入或更新文档级元数据，返回 document_id。"""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO rag_documents (
                doc_name, version, collection, business_scope, source_path,
                source_type, status, risk_level, metadata, content_hash, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, now())
            ON CONFLICT (doc_name, version, collection, business_scope)
            DO UPDATE SET
                source_path = EXCLUDED.source_path,
                source_type = EXCLUDED.source_type,
                status = EXCLUDED.status,
                risk_level = EXCLUDED.risk_level,
                metadata = EXCLUDED.metadata,
                content_hash = EXCLUDED.content_hash,
                updated_at = now()
            RETURNING id
            """,
            (
                chunk.doc_name,
                chunk.version,
                chunk.collection,
                chunk.business_scope,
                source_path,
                chunk.source_type,
                chunk.status,
                chunk.risk_level,
                json.dumps(chunk.metadata, ensure_ascii=False),
                content_hash,
            ),
        )
        return int(cursor.fetchone()[0])


def _upsert_chunk(conn: Any, *, document_id: int, chunk: KnowledgeChunk, content_hash: str) -> int:
    """写入或更新 chunk；内容变化会生成新的唯一记录，旧向量保留可追溯。"""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO rag_chunks (
                document_id, chunk_index, paragraph, content_hash, collection,
                business_scope, heading_path, status, risk_level, answerable_intents,
                source_type, effective_time, expire_time, metadata, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
            ON CONFLICT (document_id, chunk_index, content_hash)
            DO UPDATE SET
                paragraph = EXCLUDED.paragraph,
                collection = EXCLUDED.collection,
                business_scope = EXCLUDED.business_scope,
                heading_path = EXCLUDED.heading_path,
                status = EXCLUDED.status,
                risk_level = EXCLUDED.risk_level,
                answerable_intents = EXCLUDED.answerable_intents,
                source_type = EXCLUDED.source_type,
                effective_time = EXCLUDED.effective_time,
                expire_time = EXCLUDED.expire_time,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            RETURNING id
            """,
            (
                document_id,
                chunk.chunk_index,
                chunk.paragraph,
                content_hash,
                chunk.collection,
                chunk.business_scope,
                chunk.heading_path,
                chunk.status,
                chunk.risk_level,
                chunk.answerable_intents,
                chunk.source_type,
                chunk.effective_time,
                chunk.expire_time,
                json.dumps(_chunk_metadata(chunk), ensure_ascii=False),
            ),
        )
        return int(cursor.fetchone()[0])


def _embedding_exists(conn: Any, *, chunk_id: int, embedding_version: str) -> bool:
    """判断指定 chunk 的当前 embedding 版本是否已经存在。"""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM rag_embeddings WHERE chunk_id = %s AND embedding_version = %s",
            (chunk_id, embedding_version),
        )
        return cursor.fetchone() is not None


def _insert_embedding(conn: Any, *, chunk_id: int, content_hash: str, vector: list[float]) -> None:
    """写入 chunk embedding，重复版本通过唯一键跳过。"""
    config = load_pgvector_config(require_api_key=False)
    embedding = config.embedding
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO rag_embeddings (
                chunk_id, embedding_provider, embedding_model, embedding_dimension,
                embedding_distance, embedding_version, embedding, content_hash
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
            ON CONFLICT (chunk_id, embedding_version) DO NOTHING
            """,
            (
                chunk_id,
                embedding.provider,
                embedding.model,
                embedding.dimension,
                embedding.distance,
                embedding.version,
                vector_literal(vector),
                content_hash,
            ),
        )


def _reset_tables(conn: Any) -> None:
    """清空 RAG 入库表，便于本地或灰度环境重建知识库。"""
    with conn.cursor() as cursor:
        cursor.execute("TRUNCATE rag_retrieval_logs, rag_embeddings, rag_chunks, rag_documents RESTART IDENTITY CASCADE")
    conn.commit()


def _count_documents(conn: Any) -> int:
    """统计当前文档数量，作为入库摘要输出。"""
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM rag_documents")
        return int(cursor.fetchone()[0])


def _chunk_metadata(chunk: KnowledgeChunk) -> dict[str, Any]:
    """整理 chunk metadata，避免把空值和向量字段混入业务元数据。"""
    metadata = dict(chunk.metadata)
    metadata.update(
        {
            "embedding_provider": chunk.embedding_provider,
            "embedding_model": chunk.embedding_model,
            "embedding_dimension": chunk.embedding_dimension,
            "embedding_distance": chunk.embedding_distance,
            "embedding_version": chunk.embedding_version,
        }
    )
    return {key: value for key, value in metadata.items() if value is not None}


def _find_source_path(kb_dir: str, chunk: KnowledgeChunk) -> str | None:
    """根据 doc_name 找回源文件路径，便于后续排查引用来源。"""
    root = Path(kb_dir)
    for suffix in (".md", ".txt"):
        matches = list(root.rglob(f"{chunk.doc_name}{suffix}"))
        if matches:
            return str(matches[0])
    return None


def _hash_text(text: str) -> str:
    """生成 SHA-256 hash，统一用于文档和 chunk 幂等判断。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _connect(database_url: str) -> Any:
    """延迟导入 psycopg，避免 memory 后端不安装数据库依赖也能运行。"""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for pgvector ingestion") from exc
    return psycopg.connect(database_url)


if __name__ == "__main__":
    main()
