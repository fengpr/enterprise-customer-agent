"""验证 Redis 缓存命中、失效、容错和业务缓存接入。"""

from services.cache_service import CacheService
from rag.rag_chain import RagChain, normalize_retrieval_cache_query
from schemas.intent_schema import Citation


class FakeRedis:
    """最小 Redis 替身，记录缓存值与删除操作。"""
    def __init__(self): self.values, self.deleted = {}, []
    def get(self, key): return self.values.get(key)
    def setex(self, key, ttl, value): self.values[key] = value
    def delete(self, *keys): self.deleted.extend(keys)


def test_cache_hit_delete_and_singleflight_loader():
    """缓存命中不应重复回源，删除后可重新加载。"""
    cache, redis, calls = CacheService(FakeRedis()), None, []
    redis = cache.client; key = cache.key("order", customer=1, order="EC1")
    assert cache.get_or_load(key, 30, lambda: calls.append(1) or {"id": 1}, metric="order_cache_hit") == {"id": 1}
    assert cache.get_or_load(key, 30, lambda: calls.append(2) or {"id": 2}, metric="order_cache_hit") == {"id": 1}
    cache.delete(key)
    assert key in redis.deleted and calls == [1]


def test_cache_failure_falls_back_without_raising():
    """Redis 异常时缓存层必须静默降级，调用方仍可获得回源结果。"""
    class BrokenRedis:
        def get(self, key): raise RuntimeError("down")
        def setex(self, *args): raise RuntimeError("down")
    cache = CacheService(BrokenRedis())
    assert cache.get_or_load("key", 30, lambda: {"ok": True}, metric="rag_cache_hit") == {"ok": True}
    assert cache.snapshot_metrics()["cache_error"] >= 1


def test_rag_key_changes_with_knowledge_version(monkeypatch):
    """知识库版本变化必须生成不同 RAG 缓存 key。"""
    cache = CacheService(FakeRedis(), namespace="rag")
    monkeypatch.setenv("KNOWLEDGE_BASE_VERSION", "v1")
    first = cache.key("retrieval", query="退款", knowledge_base_version="v1")
    second = cache.key("retrieval", query="退款", knowledge_base_version="v2")
    assert first != second


def test_stable_knowledge_consult_queries_share_cache_keys_across_scopes():
    """不同业务域的稳定知识咨询都应使用一致的语义缓存归一化策略。"""
    first = normalize_retrieval_cache_query("查询退货规则", user_goal="policy_consult", business_scope="return_goods")
    second = normalize_retrieval_cache_query("查看退货规则？", user_goal="policy_consult", business_scope="return_goods")
    refund_policy = normalize_retrieval_cache_query("退款政策", user_goal="policy_consult", business_scope="refund")
    invoice_process = normalize_retrieval_cache_query("发票怎么开", user_goal="how_to", business_scope="invoice")

    assert first == second == "return_goods:policy_consult:rules"
    assert refund_policy == "refund:policy_consult:rules"
    assert invoice_process == "invoice:how_to:process"


def test_entity_bound_or_status_queries_keep_exact_cache_keys():
    """关联订单、工单或实时状态的问题不得被语义缓存合并。"""
    order_query = "EC202607130001这个订单可以退货吗"
    status_query = "查询订单物流进度"

    assert normalize_retrieval_cache_query(order_query, user_goal="policy_consult", business_scope="return_goods") == order_query.lower()
    assert normalize_retrieval_cache_query(status_query, user_goal="status_query", business_scope="logistics") == status_query


def test_similar_return_policy_queries_only_retrieve_once(monkeypatch):
    """同义退货规则问法应直接复用 Redis 召回结果，不重复调用向量检索。"""
    class CountingVectorStore:
        """记录向量检索次数的测试替身。"""

        def __init__(self):
            self.calls = 0

        def similarity_search(self, *_args, **_kwargs):
            self.calls += 1
            return [
                Citation(
                    doc_name="return_goods_policy",
                    version="V1",
                    paragraph="签收后 7 天内且不影响二次销售时可申请退货。",
                    score=0.9,
                    business_scope="return_goods",
                    collection="return_policy",
                )
            ]

    monkeypatch.setenv("KNOWLEDGE_BASE_VERSION", "test-v1")
    rag = object.__new__(RagChain)
    rag.cache = CacheService(FakeRedis(), namespace="rag")
    rag.vector_store = CountingVectorStore()
    payload = {"intent": "refund", "user_goal": "policy_consult", "business_scope": "return_goods"}

    first = rag._retrieve(payload | {"query": "查询退货规则"})
    second = rag._retrieve(payload | {"query": "查看退货规则？"})

    assert first and second
    assert rag.vector_store.calls == 1
