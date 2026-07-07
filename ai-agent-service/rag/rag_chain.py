import os
import warnings
from typing import Any

from langchain_core.runnables import RunnableLambda

from rag.knowledge_taxonomy import infer_business_scope
from rag.pg_config import RagConfigError
from rag.pg_vector_store import PgVectorStore
from rag.vector_store import InMemoryVectorStore
from schemas.intent_schema import Citation


class RagChain:
    """RAG 检索链，按配置选择内存基线或 pgvector 持久化检索。"""

    def __init__(self) -> None:
        """初始化检索后端，默认保持 memory，配置 pgvector 时使用数据库召回。"""
        self.backend = os.getenv("RAG_STORE_BACKEND", "memory").strip().lower()
        self.vector_store = self._build_store()
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
        return self.vector_store.similarity_search(
            query,
            intent=intent,
            user_goal=user_goal,
            business_scope=str(business_scope) if business_scope else None,
            collection=payload.get("collection"),
        )

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
