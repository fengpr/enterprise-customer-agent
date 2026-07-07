from rag.rag_chain import RagChain


class KnowledgeBaseTools:
    """知识库工具，向 Agent 暴露可审计的 RAG 检索能力。"""

    def __init__(self) -> None:
        """初始化 RAG 链，确保工具调用与回复链路使用同一检索规则。"""
        self.rag = RagChain()

    def search(self, query: str, business_scope: str | None = None, intent: str = "other") -> dict:
        """检索知识库并返回引用列表，供 Agent 生成有依据的回复。"""
        return {
            "status": "success",
            "citations": [
                item.model_dump()
                for item in self.rag.retrieve(query, intent=intent, business_scope=business_scope)
            ],
        }
