import os
import re
import warnings
from typing import Any

from langchain_core.runnables import RunnableLambda

from rag.knowledge_taxonomy import infer_business_scope
from rag.pg_config import RagConfigError
from rag.pg_vector_store import PgVectorStore
from rag.query_rewriter import LLMQueryRewriter
from rag.quality import ensure_citation_ids
from rag.vector_store import InMemoryVectorStore
from schemas.intent_schema import Citation
from services.cache_service import CacheService


class RagChain:
    """RAG 检索链，按配置选择内存基线或 pgvector 持久化检索。"""

    def __init__(self) -> None:
        """初始化检索后端，默认保持 memory，配置 pgvector 时使用数据库召回。"""
        self.backend = os.getenv("RAG_STORE_BACKEND", "memory").strip().lower()
        self.vector_store = self._build_store()
        self.cache = CacheService(namespace="rag")
        # 改写器独立配置且可用时才会触发，不影响无模型凭证的本地检索基线。
        self.query_rewriter = LLMQueryRewriter()
        self.retriever_chain = RunnableLambda(self._retrieve)

    def check_startup(self) -> None:
        """服务启动阶段只检查 pgvector schema，不执行 DDL 或入库。"""
        if isinstance(self.vector_store, PgVectorStore):
            try:
                self.vector_store.check_schema()
            except Exception as exc:
                if _strict_startup():
                    raise
                warnings.warn(f"RAG pgvector schema check failed, fallback to memory store: {exc}", RuntimeWarning)
                self.backend = "memory"
                self.vector_store = InMemoryVectorStore()

    def retrieve(
        self,
        query: str | dict,
        *,
        intent: str = "other",
        user_goal: str = "other",
        business_scope: str | None = None,
        collection: str | None = None,
    ) -> list[Citation]:
        """对外提供知识库检索入口，返回可用于回复引用的片段。"""
        if isinstance(query, dict):
            return self.retriever_chain.invoke(query)
        return self.retriever_chain.invoke(
            {
                "query": query,
                "intent": intent,
                "user_goal": user_goal,
                "business_scope": business_scope,
                "collection": collection,
            }
        )

    def _retrieve(self, payload: str | dict[str, Any]) -> list[Citation]:
        """按业务范围检索知识库，避免所有文档混在一起召回。"""
        if isinstance(payload, str):
            payload = {"query": payload, "intent": "other", "user_goal": "other"}
        query = str(payload.get("query") or payload.get("message") or "")
        intent = str(payload.get("intent") or "other")
        user_goal = str(payload.get("user_goal") or "other")
        business_scope = payload.get("business_scope") or infer_business_scope(intent, user_goal)
        scope = str(business_scope) if business_scope else None
        normalized_query = normalize_retrieval_cache_query(query, user_goal=user_goal, business_scope=scope)
        cache_key = self.cache.key("retrieval", query=normalized_query, intent=intent, scope=scope, collection=payload.get("collection"), top_k=int(os.getenv("RAG_TOP_K", "5")), knowledge_base_version=os.getenv("KNOWLEDGE_BASE_VERSION", "v1"), embedding_model_version=os.getenv("EMBEDDING_MODEL_VERSION", "default"))
        cached = self.cache.get(cache_key, metric="rag_cache_hit")
        if cached is not None:
            # 命中结果不再调用重写模型；保留原始模式以便离线审计区分。
            citations = [Citation.model_validate(item) for item in cached]
            return ensure_citation_ids(_mark_cache_hit(citations))

        def load() -> list[dict[str, Any]]:
            retrieval_query, rewrite_mode = self._resolve_retrieval_query(
                query=query,
                intent=intent,
                user_goal=user_goal,
                business_scope=scope,
                conversation_context=payload.get("conversation_context"),
            )
            citations = self.vector_store.similarity_search(
                retrieval_query,
                intent=intent,
                user_goal=user_goal,
                business_scope=scope,
                collection=payload.get("collection"),
                rewrite_mode=rewrite_mode,
            )
            return [citation.model_dump() for citation in citations]
        # 缓存键已包含知识库和嵌入模型版本，默认可安全保留 15 分钟。
        cached = self.cache.get_or_load(cache_key, int(os.getenv("RAG_CACHE_TTL_SECONDS", "900")), load, metric="rag_cache_hit")
        citations = [Citation.model_validate(item) for item in (cached or [])]
        # 所有检索后端统一补齐片段 ID，确保后续生成引用可验证。
        return ensure_citation_ids(citations)

    def _resolve_retrieval_query(
        self,
        *,
        query: str,
        intent: str,
        user_goal: str,
        business_scope: str | None,
        conversation_context: dict[str, Any] | None,
    ) -> tuple[str, str]:
        """仅在非实体知识咨询缓存未命中后调用 LLM；失败继续使用原始问题和规则扩展。"""
        if not _should_use_llm_rewrite(query, user_goal=user_goal):
            return query, "rule_fallback"
        rewriter = getattr(self, "query_rewriter", None)
        if rewriter is None:
            return query, "rule_fallback"
        result = rewriter.rewrite(
            query=query,
            intent=intent,
            user_goal=user_goal,
            business_scope=business_scope,
            conversation_context=conversation_context,
        )
        if result is None:
            return query, "rule_fallback"
        return result.rewritten_query, "llm_hybrid"

    def _build_store(self):
        """根据 RAG_STORE_BACKEND 创建检索后端，生产严格模式下禁止静默降级。"""
        if self.backend in {"", "memory"}:
            return InMemoryVectorStore()
        if self.backend != "pgvector":
            raise RagConfigError(f"Unsupported RAG_STORE_BACKEND: {self.backend}")
        try:
            return PgVectorStore()
        except Exception as exc:
            if _strict_startup():
                raise
            warnings.warn(f"RAG pgvector init failed, fallback to memory store: {exc}", RuntimeWarning)
            self.backend = "memory"
            return InMemoryVectorStore()


def _strict_startup() -> bool:
    """读取严格启动开关；生产环境应设置为 true。"""
    return os.getenv("RAG_STRICT_STARTUP", "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_retrieval_cache_query(query: str, *, user_goal: str, business_scope: str | None) -> str:
    """归一化检索缓存查询；对稳定知识咨询按业务域和问题焦点复用召回结果。"""
    normalized = " ".join(str(query or "").lower().split())
    compact = re.sub(r"[\s，,。、？?!！：:]+", "", normalized)
    # 实时状态、业务动作、订单/工单编号均与单个实体强绑定，只能使用精确文本缓存。
    if user_goal not in {"policy_consult", "how_to"} or not business_scope or _has_entity_bound_reference(compact):
        return normalized

    focus = _knowledge_consult_focus(compact)
    if focus:
        # intent/user_goal、scope、知识库版本和模型版本仍会组成完整 key，此处只归一化语义等价的问法。
        return f"{business_scope}:{user_goal}:{focus}"
    return normalized


def _knowledge_consult_focus(query: str) -> str | None:
    """区分规则、条件、流程和时效等知识咨询焦点，避免不同问法互相污染。"""
    if any(term in query for term in ["多久", "几天", "时效", "到账", "时间", "什么时候"]):
        return "timing"
    if any(term in query for term in ["怎么", "如何", "流程", "步骤", "申请", "办理", "开具", "入口"]):
        return "process"
    if any(term in query for term in ["条件", "资格", "适用", "是否", "能不能", "可以", "要求"]):
        return "eligibility"
    if any(term in query for term in ["规则", "政策", "规定", "权益", "费用", "运费"]):
        return "rules"
    return None


def _has_entity_bound_reference(query: str) -> bool:
    """判断问题是否搬带具体订单、工单等实体，此类问题不参与语义缓存合并。"""
    return bool(
        re.search(r"(?<![a-z])(?:ec)?\d{10,18}", query, flags=re.IGNORECASE)
        or re.search(r"\bt\d{6,}\b", query, flags=re.IGNORECASE)
        or any(term in query for term in ["我的订单", "这个订单", "这笔订单", "当前订单", "工单号"])
    )


def _should_use_llm_rewrite(query: str, *, user_goal: str) -> bool:
    """实体状态和动作流程必须保留原文，只有知识问答可承担额外语义改写。"""
    return user_goal in {"policy_consult", "how_to"} and not _has_entity_bound_reference(query)


def _mark_cache_hit(citations: list[Citation]) -> list[Citation]:
    """为缓存返回的引用标记命中模式，不覆盖首次检索的真实改写来源。"""
    for citation in citations:
        metadata = dict(citation.metadata or {})
        metadata.setdefault("query_rewrite_origin_mode", metadata.get("query_rewrite_mode", "rule_fallback"))
        metadata["query_rewrite_mode"] = "cache_hit"
        citation.metadata = metadata
    return citations
