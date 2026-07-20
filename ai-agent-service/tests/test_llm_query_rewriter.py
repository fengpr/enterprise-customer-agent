"""验证 LLM 驱动 RAG 查询重写的范围、降级和缓存行为。"""

import json

from rag.query_rewriter import LLMQueryRewriter
from rag.rag_chain import RagChain
from rag.vector_store import InMemoryVectorStore
from schemas.intent_schema import Citation
from services.cache_service import CacheService
from services.resilient_client import ResilienceError


class FakeRedis:
    """最小缓存替身，避免专项测试依赖本地 Redis。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str):
        return self.values.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.values[key] = value


class DirectInvoker:
    """直接执行操作的韧性替身，隔离真实网络调用。"""

    def invoke(self, operation):
        return operation()


class RecordingStore:
    """记录最终送入检索后端的 Query 与改写模式。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def similarity_search(self, query: str, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return [
            Citation(
                doc_name="退款政策",
                version="V1",
                paragraph="退款审核通过后通常会原路退回。",
                score=0.9,
                business_scope="refund",
                collection="refund_policy",
                metadata={"rewritten_query": query, "query_rewrite_mode": kwargs.get("rewrite_mode")},
            )
        ]


class RecordingRewriter:
    """记录 RAG 是否调用重写器，并模拟一个通过校验的语义补全结果。"""

    def __init__(self, result):
        self.calls: list[dict] = []
        self.result = result

    def rewrite(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _rag(store: RecordingStore, rewriter: RecordingRewriter) -> RagChain:
    """构造不初始化真实后端的 RAG 实例。"""
    rag = object.__new__(RagChain)
    rag.cache = CacheService(FakeRedis(), namespace="rag")
    rag.vector_store = store
    rag.query_rewriter = rewriter
    return rag


def test_rewriter_uses_independent_downstream_and_redacts_previous_turn(monkeypatch):
    """重写器只接收安全上下文，且使用独立的 rag_query_rewrite 舱壁。"""
    captured: list[dict] = []
    monkeypatch.setenv("RAG_LLM_QUERY_REWRITE_ENABLED", "true")
    rewriter = LLMQueryRewriter(
        invoker=DirectInvoker(),
        invoke_rewrite=lambda payload: captured.append(payload) or json.dumps(
            {"rewritten_query": "退款审核通过后多久到账", "confidence": 0.91}
        ),
    )

    result = rewriter.rewrite(
        query="多久到账",
        intent="refund",
        user_goal="policy_consult",
        business_scope="refund",
        conversation_context={
            "login_user_context": {"display_name": "张三", "customer_id": 1},
            "session_memory": {
                "last_user_question": "订单 EC202607130001 的退款什么时候到账",
                "last_ai_answer": "当前登录账号显示为张三，工单 T202607130001 正在处理。",
            },
            "Authorization": "Bearer should-not-leak",
        },
    )

    assert result and result.rewritten_query == "退款审核通过后多久到账"
    assert captured and "EC202607130001" not in captured[0]["previous_user_question"]
    assert "T202607130001" not in captured[0]["previous_ai_answer"]
    assert "张三" not in captured[0]["previous_ai_answer"]
    assert "Authorization" not in captured[0]


def test_rewriter_rejects_sensitive_or_low_confidence_output(monkeypatch):
    """低置信度或携带实体标识的输出必须触发规则改写兜底。"""
    monkeypatch.setenv("RAG_LLM_QUERY_REWRITE_ENABLED", "true")
    rewriter = LLMQueryRewriter(
        invoker=DirectInvoker(),
        invoke_rewrite=lambda _payload: '{"rewritten_query":"查询 EC202607130001 的退款","confidence":0.95}',
    )

    assert rewriter.rewrite(query="退款规则", intent="refund", user_goal="policy_consult", business_scope="refund") is None


def test_cache_miss_uses_llm_query_then_cache_hit_skips_rewriter():
    """同义规则咨询第一次混合改写，第二次直接复用缓存且不再调用模型。"""
    from rag.query_rewriter import QueryRewriteResult

    store = RecordingStore()
    rewriter = RecordingRewriter(QueryRewriteResult("退款审核通过后到账时间", 0.93))
    rag = _rag(store, rewriter)
    payload = {"intent": "refund", "user_goal": "policy_consult", "business_scope": "refund"}

    first = rag._retrieve(payload | {"query": "退款多久到账"})
    second = rag._retrieve(payload | {"query": "退款到账需要几天"})

    assert len(rewriter.calls) == 1
    assert len(store.calls) == 1
    assert store.calls[0]["query"] == "退款审核通过后到账时间"
    assert store.calls[0]["rewrite_mode"] == "llm_hybrid"
    assert first[0].metadata["query_rewrite_mode"] == "llm_hybrid"
    assert second[0].metadata["query_rewrite_mode"] == "cache_hit"
    assert second[0].metadata["query_rewrite_origin_mode"] == "llm_hybrid"


def test_non_knowledge_and_entity_queries_never_call_llm_rewriter():
    """状态查询、动作请求和订单实体引用必须保持原文检索，不额外调用模型。"""
    from rag.query_rewriter import QueryRewriteResult

    store = RecordingStore()
    rewriter = RecordingRewriter(QueryRewriteResult("不应被使用", 0.95))
    rag = _rag(store, rewriter)

    rag._retrieve({"query": "查询订单物流状态", "intent": "logistics", "user_goal": "status_query", "business_scope": "logistics"})
    rag._retrieve({"query": "我要退货", "intent": "refund", "user_goal": "action_request", "business_scope": "return_goods"})
    rag._retrieve({"query": "EC202607130001 可以退货吗", "intent": "refund", "user_goal": "policy_consult", "business_scope": "return_goods"})

    assert rewriter.calls == []
    assert [item["rewrite_mode"] for item in store.calls] == ["rule_fallback", "rule_fallback", "rule_fallback"]


def test_rewriter_failure_falls_back_to_original_query_and_rule_mode():
    """模型不可用时 RAG 仍正常检索，调用方无需进入客户侧降级。"""
    store = RecordingStore()
    rag = _rag(store, RecordingRewriter(None))

    results = rag._retrieve({"query": "退款规则", "intent": "refund", "user_goal": "policy_consult", "business_scope": "refund"})

    assert store.calls[0]["query"] == "退款规则"
    assert store.calls[0]["rewrite_mode"] == "rule_fallback"
    assert results[0].metadata["query_rewrite_mode"] == "rule_fallback"


def test_llm_rewrite_still_passes_through_business_scope_rule_expansion():
    """LLM 负责语义补全，向量检索前仍要由规则补齐稳定的业务范围词。"""
    from rag.query_rewriter import QueryRewriteResult

    rewriter = RecordingRewriter(QueryRewriteResult("退款审核通过后到账时间", 0.93))
    rag = _rag(InMemoryVectorStore(), rewriter)

    results = rag._retrieve({"query": "多久到账", "intent": "refund", "user_goal": "policy_consult", "business_scope": "refund"})

    final_query = results[0].metadata["rewritten_query"]
    assert "退款审核通过后到账时间" in final_query
    assert "原路退回" in final_query
    assert results[0].metadata["query_rewrite_mode"] == "llm_hybrid"


def test_rewriter_downstream_error_returns_none_without_breaking_rag(monkeypatch):
    """429、超时和熔断等标准错误由重写器吞掉，RAG 继续走规则检索。"""
    monkeypatch.setenv("RAG_LLM_QUERY_REWRITE_ENABLED", "true")
    rewriter = LLMQueryRewriter(
        invoker=DirectInvoker(),
        invoke_rewrite=lambda _payload: (_ for _ in ()).throw(
            ResilienceError("rag_query_rewrite", "rate_limit_429", True, status_code=429)
        ),
    )

    assert rewriter.rewrite(query="退款规则", intent="refund", user_goal="policy_consult", business_scope="refund") is None
