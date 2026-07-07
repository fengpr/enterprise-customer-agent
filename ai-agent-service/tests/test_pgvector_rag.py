"""验证 pgvector RAG 后端的配置、schema、阈值和启动策略。"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.pg_config import EmbeddingConfig, PgVectorConfig, RagConfigError, load_embedding_config
from rag.pg_ingest import content_hash
from rag.pg_schema import schema_sql
from rag.pg_vector_store import PgVectorStore
from rag.rag_chain import RagChain
from rag.text_processing import KnowledgeChunk


def _embedding_config() -> EmbeddingConfig:
    """构造测试用 embedding 配置，避免依赖真实环境变量。"""
    return EmbeddingConfig(
        provider="openai",
        model="text-embedding-3-small",
        dimension=3,
        distance="cosine",
        version="test:embedding:3:cosine",
        api_key="test-key",
        base_url="https://example.test/v1",
        batch_size=2,
        timeout=1.0,
        max_retries=0,
    )


def _pg_config(*, min_score: float = 0.55, answerable_filter: bool = False) -> PgVectorConfig:
    """构造测试用 pgvector 配置。"""
    return PgVectorConfig(
        database_url="postgresql://user:pass@localhost:5432/test",
        top_k_recall=20,
        top_n_context=5,
        min_similarity_score=min_score,
        answerable_intent_filter=answerable_filter,
        strict_startup=False,
        embedding=_embedding_config(),
    )


class _FakeEmbeddingClient:
    """测试用 embedding 客户端，避免真实网络请求。"""

    def embed_query(self, text: str) -> list[float]:
        """返回固定查询向量。"""
        return [0.1, 0.2, 0.3]


class PgVectorRagTest(unittest.TestCase):
    """覆盖 pgvector 第一版的关键安全边界。"""

    def test_schema_sql_contains_required_tables_and_hnsw_index(self):
        """schema SQL 必须包含三表拆分、检索日志和 HNSW cosine 索引。"""
        sql = schema_sql(dimension=3)

        self.assertIn("rag_documents", sql)
        self.assertIn("rag_chunks", sql)
        self.assertIn("rag_embeddings", sql)
        self.assertIn("rag_retrieval_logs", sql)
        self.assertIn("USING hnsw", sql)
        self.assertIn("vector_cosine_ops", sql)

    def test_embedding_config_requires_key_when_used(self):
        """真实 embedding 调用场景必须显式配置 API Key。"""
        with patch.dict(os.environ, {"EMBEDDING_PROVIDER": "openai"}, clear=True):
            with self.assertRaises(RagConfigError):
                load_embedding_config(require_api_key=True)

    def test_content_hash_is_stable_for_same_chunk(self):
        """相同 chunk 生成相同 content_hash，支持失败后重跑跳过。"""
        chunk = KnowledgeChunk(
            doc_name="refund_policy",
            version="V1",
            paragraph="退款审核通过后通常 1-7 个工作日到账。",
            collection="refund_policy",
            business_scope="refund",
            risk_level="medium",
            answerable_intents=["refund"],
        )

        self.assertEqual(content_hash(chunk), content_hash(chunk))

    def test_pgvector_returns_empty_when_best_score_below_threshold(self):
        """最高相似度低于阈值时必须返回空 citations，触发 no_kb_hit 风控。"""
        store = PgVectorStore(_pg_config(min_score=0.55), _FakeEmbeddingClient())
        store._query_rows = lambda **_: [_row(similarity=0.42)]  # type: ignore[method-assign]
        store._log_retrieval = lambda **_: None  # type: ignore[method-assign]

        results = store.similarity_search("退款多久到账", intent="refund", business_scope="refund")

        self.assertEqual(results, [])

    def test_pgvector_returns_context_when_score_passes_threshold(self):
        """相似度达到阈值后才允许返回最终上下文片段。"""
        store = PgVectorStore(_pg_config(min_score=0.55), _FakeEmbeddingClient())
        store._query_rows = lambda **_: [_row(similarity=0.81)]  # type: ignore[method-assign]
        store._log_retrieval = lambda **_: None  # type: ignore[method-assign]

        results = store.similarity_search("退款多久到账", intent="refund", business_scope="refund")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].retrieval_source, "pgvector_cosine")
        self.assertGreaterEqual(results[0].score, 0.55)

    def test_pgvector_filters_customer_invisible_chunks_after_rerank(self):
        """pgvector 返回上下文前应过滤适用范围、标准话术和 AI 内部约束片段。"""
        store = PgVectorStore(_pg_config(min_score=0.55), _FakeEmbeddingClient())
        store._query_rows = lambda **_: [
            _row(
                similarity=0.92,
                paragraph="本文档适用于客户咨询退货条件。适合回答的典型表达包括：“我想退货”。",
                heading_path=["退货条件政策", "适用范围"],
                business_scope="return_goods",
            ),
            _row(
                similarity=0.88,
                paragraph="AI 应说明需要结合商品规则和订单状态判断，不得直接承诺一定可退。",
                heading_path=["退货条件政策", "内部处理分支"],
                business_scope="return_goods",
            ),
            _row(
                similarity=0.84,
                paragraph="签收后 7 天内且商品不影响二次销售时，可申请退货；是否通过以售后审核结果为准。",
                heading_path=["退货条件政策", "客户可见规则"],
                business_scope="return_goods",
            ),
        ]  # type: ignore[method-assign]
        store._log_retrieval = lambda **_: None  # type: ignore[method-assign]

        results = store.similarity_search(
            "查看退货规则",
            intent="refund",
            user_goal="policy_consult",
            business_scope="return_goods",
        )

        self.assertEqual(len(results), 1)
        self.assertIn("签收后 7 天内", results[0].paragraph)
        self.assertNotIn("适合回答", results[0].paragraph)
        self.assertNotIn("AI 应", results[0].paragraph)

    def test_answerable_intent_filter_defaults_to_rerank_signal_only(self):
        """answerable_intents 第一版默认不做强过滤，避免意图识别错误导致无召回。"""
        self.assertFalse(_pg_config().answerable_intent_filter)
        self.assertTrue(_pg_config(answerable_filter=True).answerable_intent_filter)

    def test_rag_chain_falls_back_to_memory_only_when_not_strict(self):
        """本地开发可回退 memory，生产严格模式必须失败。"""
        with patch.dict(os.environ, {"RAG_STORE_BACKEND": "pgvector", "RAG_STRICT_STARTUP": "false"}, clear=True):
            rag = RagChain()
            self.assertEqual(rag.backend, "memory")

        with patch.dict(os.environ, {"RAG_STORE_BACKEND": "pgvector", "RAG_STRICT_STARTUP": "true"}, clear=True):
            with self.assertRaises(RagConfigError):
                RagChain()


def _row(
    *,
    similarity: float,
    paragraph: str = "退款审核通过后通常 1-7 个工作日原路退回。",
    heading_path: list[str] | None = None,
    business_scope: str = "refund",
) -> dict:
    """构造 pgvector 查询返回行。"""
    return {
        "chunk_id": 1,
        "doc_name": "refund_arrival_policy",
        "version": "V1",
        "paragraph": paragraph,
        "collection": "refund_policy",
        "business_scope": business_scope,
        "heading_path": heading_path or ["退款到账政策", "客户可见规则"],
        "risk_level": "medium",
        "answerable_intents": ["refund", "consult"],
        "metadata": {},
        "embedding_version": "test:embedding:3:cosine",
        "embedding_provider": "openai",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimension": 3,
        "embedding_distance": "cosine",
        "vector_distance": 1 - similarity,
        "similarity_score": similarity,
    }


if __name__ == "__main__":
    unittest.main()
