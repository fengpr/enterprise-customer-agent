import json
import time
from typing import Any

from rag.embeddings import OpenAICompatibleEmbeddingClient, vector_literal
from rag.knowledge_taxonomy import collections_for_intent, infer_business_scope
from rag.pg_config import PgVectorConfig, RagConfigError, load_pgvector_config
from rag.pg_schema import check_schema
from rag.vector_store import is_customer_visible_chunk, rewrite_query
from schemas.intent_schema import Citation


class PgVectorStore:
    """Postgres + pgvector 检索后端，提供与内存 store 兼容的检索接口。"""

    def __init__(
        self,
        config: PgVectorConfig | None = None,
        embedding_client: OpenAICompatibleEmbeddingClient | None = None,
    ) -> None:
        """初始化 pgvector store；只读取配置，不自动建表或入库。"""
        self.config = config or load_pgvector_config(require_api_key=True)
        self.embedding_client = embedding_client or OpenAICompatibleEmbeddingClient(self.config.embedding)

    def check_schema(self) -> None:
        """服务启动检查 schema，不执行 DDL。"""
        check_schema(self.config.database_url)

    def similarity_search(
        self,
        query: str,
        *,
        intent: str = "other",
        user_goal: str = "other",
        business_scope: str | None = None,
        collection: str | None = None,
        top_k: int = 5,
        rewrite_mode: str = "rule_fallback",
    ) -> list[Citation]:
        """执行 pgvector 检索，并在低于阈值时返回空 citations 触发 no_kb_hit。"""
        started = time.perf_counter()
        rewritten_query = rewrite_query(query, business_scope=business_scope, intent=intent)
        resolved_scope = business_scope or infer_business_scope(intent, user_goal)
        allowed_collections = [collection] if collection else collections_for_intent(intent, resolved_scope)
        top_k_recall = max(top_k, self.config.top_k_recall)
        top_n_context = min(top_k, self.config.top_n_context) if top_k else self.config.top_n_context
        filters = {
            "intent": intent,
            "user_goal": user_goal,
            "business_scope": resolved_scope,
            "rewritten_query": rewritten_query,
            "query_rewrite_mode": rewrite_mode,
            "collection": collection,
            "allowed_collections": allowed_collections,
            "answerable_intent_filter": self.config.answerable_intent_filter,
            "min_similarity_score": self.config.min_similarity_score,
        }

        query_vector = self.embedding_client.embed_query(rewritten_query)
        rows = self._query_rows(
            query_vector=query_vector,
            intent=intent,
            resolved_scope=resolved_scope,
            allowed_collections=allowed_collections,
            top_k=top_k_recall,
        )
        ranked = self._rerank(rows, rewritten_query=rewritten_query, intent=intent, resolved_scope=resolved_scope)
        ranked = [
            row
            for row in ranked
            if is_customer_visible_chunk(row.get("paragraph") or "", row.get("heading_path") or [])
        ]
        best_similarity = max((row["similarity_score"] for row in ranked), default=0.0)
        no_hit = not ranked or best_similarity < self.config.min_similarity_score
        selected = [] if no_hit else ranked[:top_n_context]
        citations = [
            self._to_citation(row, query=rewritten_query, final_rank=index + 1, rewrite_mode=rewrite_mode)
            for index, row in enumerate(selected)
        ]
        self._log_retrieval(
            query=query,
            filters=filters,
            top_k=top_k_recall,
            rows=ranked,
            selected=selected,
            latency_ms=int((time.perf_counter() - started) * 1000),
            no_hit=no_hit,
        )
        return citations

    def _query_rows(
        self,
        *,
        query_vector: list[float],
        intent: str,
        resolved_scope: str,
        allowed_collections: list[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """按 metadata filter 和向量相似度从 Postgres 召回候选 chunk。"""
        vector = vector_literal(query_vector)
        sql = """
            SELECT
                c.id AS chunk_id,
                d.doc_name,
                d.version,
                c.paragraph,
                c.collection,
                c.business_scope,
                c.heading_path,
                c.risk_level,
                c.answerable_intents,
                c.metadata,
                e.embedding_version,
                e.embedding_provider,
                e.embedding_model,
                e.embedding_dimension,
                e.embedding_distance,
                (e.embedding <=> %s::vector) AS vector_distance,
                (1 - (e.embedding <=> %s::vector)) AS similarity_score
            FROM rag_embeddings e
            JOIN rag_chunks c ON c.id = e.chunk_id
            JOIN rag_documents d ON d.id = c.document_id
            WHERE e.embedding_version = %s
              AND c.status = 'PUBLISHED'
              AND c.collection = ANY(%s)
              AND (%s = 'general' OR c.business_scope = %s OR (%s = 'return_goods' AND c.business_scope = 'refund'))
              AND (c.effective_time IS NULL OR c.effective_time <= now())
              AND (c.expire_time IS NULL OR c.expire_time > now())
        """
        params: list[Any] = [
            vector,
            vector,
            self.config.embedding.version,
            allowed_collections,
            resolved_scope,
            resolved_scope,
            resolved_scope,
        ]
        if self.config.answerable_intent_filter:
            sql += " AND (cardinality(c.answerable_intents) = 0 OR %s = ANY(c.answerable_intents) OR 'consult' = ANY(c.answerable_intents))"
            params.append(intent)
        sql += " ORDER BY e.embedding <=> %s::vector LIMIT %s"
        params.extend([vector, top_k])

        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                columns = [column.name for column in cursor.description]
                return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def _rerank(
        self,
        rows: list[dict[str, Any]],
        *,
        rewritten_query: str,
        intent: str,
        resolved_scope: str,
    ) -> list[dict[str, Any]]:
        """在向量相似度基础上叠加业务 scope、标题和 answerable_intents 信号。"""
        query_terms = [item for item in rewritten_query.split() if item]
        ranked: list[dict[str, Any]] = []
        for row in rows:
            score = float(row["similarity_score"])
            if row["business_scope"] == resolved_scope:
                score += 0.08
            if resolved_scope == "return_goods":
                # 退货规则咨询优先命中退货条件/七天无理由，避免退款到账或维修政策抢占首位。
                policy_text = f"{' '.join(row.get('heading_path') or [])} {row.get('paragraph') or ''}"
                if row["business_scope"] == "refund" and not any(term in policy_text for term in ["退货", "七天无理由", "二次销售"]):
                    score -= 0.08
                if any(term in policy_text for term in ["退货", "七天无理由", "二次销售", "签收后"]):
                    score += 0.06
            if intent in (row.get("answerable_intents") or []):
                score += 0.04
            heading_text = " ".join(row.get("heading_path") or [])
            if any(term and term in heading_text for term in query_terms):
                score += 0.03
            if row["risk_level"] in {"high", "critical"}:
                score -= 0.02
            row = dict(row)
            row["rerank_score"] = round(score, 6)
            ranked.append(row)
        ranked.sort(key=lambda item: item["rerank_score"], reverse=True)
        return ranked

    def _to_citation(self, row: dict[str, Any], *, query: str, final_rank: int, rewrite_mode: str = "rule_fallback") -> Citation:
        """把数据库行转换为 Agent 可消费的 Citation，隐藏原始向量。"""
        metadata = dict(row.get("metadata") or {})
        metadata.update(
            {
                "chunk_id": row["chunk_id"],
                "embedding_version": row["embedding_version"],
                "embedding_provider": row["embedding_provider"],
                "embedding_model": row["embedding_model"],
                "embedding_dimension": row["embedding_dimension"],
                "embedding_distance": row["embedding_distance"],
                "vector_distance": float(row["vector_distance"]),
                "similarity_score": float(row["similarity_score"]),
                "rerank_score": float(row["rerank_score"]),
                "rewritten_query": query,
                "query_rewrite_mode": rewrite_mode,
                "final_rank": final_rank,
            }
        )
        return Citation(
            doc_name=row["doc_name"],
            version=row["version"],
            paragraph=row["paragraph"],
            score=round(float(row["similarity_score"]), 4),
            collection=row["collection"],
            business_scope=row["business_scope"],
            heading_path=list(row.get("heading_path") or []),
            risk_level=row["risk_level"],
            answerable_intents=list(row.get("answerable_intents") or []),
            retrieval_source="pgvector_cosine",
            metadata=metadata,
        )

    def _log_retrieval(
        self,
        *,
        query: str,
        filters: dict[str, Any],
        top_k: int,
        rows: list[dict[str, Any]],
        selected: list[dict[str, Any]],
        latency_ms: int,
        no_hit: bool,
    ) -> None:
        """记录检索日志；日志失败不影响客户回复链路。"""
        try:
            scores = [
                {
                    "chunk_id": row["chunk_id"],
                    "similarity_score": float(row["similarity_score"]),
                    "rerank_score": float(row["rerank_score"]),
                }
                for row in rows[:top_k]
            ]
            selected_chunk_ids = [row["chunk_id"] for row in selected]
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO rag_retrieval_logs (
                            query, filters, top_k, scores, selected_chunk_ids,
                            latency_ms, no_hit, embedding_version
                        )
                        VALUES (%s, %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s)
                        """,
                        (
                            query,
                            json.dumps(filters, ensure_ascii=False),
                            top_k,
                            json.dumps(scores, ensure_ascii=False),
                            selected_chunk_ids,
                            latency_ms,
                            no_hit,
                            self.config.embedding.version,
                        ),
                    )
                conn.commit()
        except Exception:
            return

    def _connect(self) -> Any:
        """延迟导入 psycopg，保持 memory 后端无需数据库依赖。"""
        try:
            import psycopg
        except ImportError as exc:
            raise RagConfigError("psycopg is required for pgvector backend") from exc
        return psycopg.connect(self.config.database_url)
