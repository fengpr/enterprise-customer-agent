import math
import re
from datetime import datetime
from typing import Any

from rag.knowledge_taxonomy import collections_for_intent, infer_business_scope
from rag.text_processing import KnowledgeChunk
from schemas.intent_schema import Citation


class InMemoryVectorStore:
    """内存知识库基线，实现 metadata filter、BM25 检索和规则 rerank。"""

    def __init__(self, chunks: list[KnowledgeChunk] | None = None) -> None:
        """初始化示例知识片段，保持本地无数据库时仍可验证 RAG 链路。"""
        self.chunks = chunks or _default_chunks()
        self._doc_freq = self._build_doc_frequency(self.chunks)
        self._avg_doc_len = sum(len(_tokenize(chunk.paragraph)) for chunk in self.chunks) / max(len(self.chunks), 1)

    def similarity_search(
        self,
        query: str,
        *,
        intent: str = "other",
        user_goal: str = "other",
        business_scope: str | None = None,
        collection: str | None = None,
        top_k: int = 5,
    ) -> list[Citation]:
        """执行带业务过滤的 BM25 基线检索，并返回可引用片段。"""
        rewritten_query = rewrite_query(query, business_scope=business_scope, intent=intent)
        resolved_scope = business_scope or infer_business_scope(intent, user_goal)
        allowed_collections = [collection] if collection else collections_for_intent(intent, resolved_scope)
        candidates = [
            chunk
            for chunk in self.chunks
            if _is_published_now(chunk)
            and is_customer_visible_chunk(chunk.paragraph, chunk.heading_path)
            and chunk.collection in allowed_collections
            and (resolved_scope == "general" or chunk.business_scope == resolved_scope or resolved_scope in chunk.metadata.get("aliases", []))
            and (not chunk.answerable_intents or intent in chunk.answerable_intents or "consult" in chunk.answerable_intents)
        ]

        scored: list[tuple[KnowledgeChunk, float, float]] = []
        for chunk in candidates:
            bm25_score = self._bm25(rewritten_query, chunk.paragraph)
            rerank_score = _rerank_score(rewritten_query, chunk, bm25_score, resolved_scope)
            if rerank_score > 0:
                scored.append((chunk, bm25_score, rerank_score))

        scored.sort(key=lambda item: item[2], reverse=True)
        return [
            _to_citation(chunk, score=_normalize_score(score), bm25_score=bm25_score, query=rewritten_query)
            for chunk, bm25_score, score in scored[:top_k]
        ]

    def _bm25(self, query: str, text: str) -> float:
        """用简化 BM25 形成可解释的全文检索基线。"""
        query_terms = _tokenize(query)
        doc_terms = _tokenize(text)
        if not query_terms or not doc_terms:
            return 0.0
        term_counts = {term: doc_terms.count(term) for term in set(doc_terms)}
        score = 0.0
        k1 = 1.5
        b = 0.75
        doc_len = len(doc_terms)
        total_docs = max(len(self.chunks), 1)
        for term in query_terms:
            tf = term_counts.get(term, 0)
            if tf == 0:
                continue
            df = self._doc_freq.get(term, 0)
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1 - b + b * doc_len / max(self._avg_doc_len, 1))
            score += idf * (tf * (k1 + 1)) / denominator
        return score

    @staticmethod
    def _build_doc_frequency(chunks: list[KnowledgeChunk]) -> dict[str, int]:
        """统计每个 token 出现在多少 chunk 中。"""
        doc_freq: dict[str, int] = {}
        for chunk in chunks:
            for term in set(_tokenize(chunk.paragraph)):
                doc_freq[term] = doc_freq.get(term, 0) + 1
        return doc_freq


def rewrite_query(query: str, *, business_scope: str | None, intent: str) -> str:
    """规则化 query rewrite，补充业务范围词但保留客户当前诉求。"""
    scope_terms = {
        "logistics": "物流 配送 发货 签收",
        "refund": "退款 退货 到账 售后",
        "return_goods": "退货 售后 七天无理由",
        "exchange": "换货 更换 售后",
        "repair": "维修 报修 故障",
        "invoice": "发票 开票 抬头 税号",
        "member": "会员 权益 等级",
        "complaint": "投诉 举报 维权 人工处理",
        "compensation_dispute": "赔付 赔偿 争议 人工审核",
        "general": "",
    }
    # 退货规则咨询必须只补充退货相关词，避免把保修、维修、质保词带入召回。
    scope_terms.update(
        {
            "refund": "退款 到账 原路退回 审核通过",
            "return_goods": "退货规则 退货条件 七天无理由 签收后 不影响二次销售 退货申请",
        }
    )
    scope = business_scope or infer_business_scope(intent)
    extra = scope_terms.get(scope, "")
    return f"{query} {extra}".strip()


def _rerank_score(query: str, chunk: KnowledgeChunk, bm25_score: float, business_scope: str) -> float:
    """规则 rerank：综合 BM25、业务范围、标题命中、风险和新鲜度。"""
    score = bm25_score
    if chunk.business_scope == business_scope:
        score += 1.2
    if any(term in " ".join(chunk.heading_path) for term in _tokenize(query)):
        score += 0.5
    if chunk.risk_level in {"high", "critical"}:
        score -= 0.2
    if chunk.source_type == "official_policy":
        score += 0.3
    return score


def is_customer_visible_chunk(paragraph: str, heading_path: list[str] | tuple[str, ...] | None = None) -> bool:
    """判断知识片段是否适合进入客户回复，过滤检索说明、示例表达和内部话术约束。"""
    text = f"{' '.join(heading_path or [])} {paragraph or ''}"
    blocked_terms = [
        "适用范围",
        "适合回答",
        "典型表达",
        "检索提示",
        "禁止话术",
        "可用标准话术",
        "标准回复要点",
        "本文档适用于",
        "本文档只说明",
        "AI 应",
        "AI 不得",
        "不得直接承诺",
    ]
    if any(term in text for term in blocked_terms):
        return False
    return bool((paragraph or "").strip())


def _to_citation(chunk: KnowledgeChunk, *, score: float, bm25_score: float, query: str) -> Citation:
    """把内部 chunk 转换为 Agent 使用的引用对象。"""
    return Citation(
        doc_name=chunk.doc_name,
        version=chunk.version,
        paragraph=chunk.paragraph,
        score=score,
        collection=chunk.collection,
        business_scope=chunk.business_scope,
        heading_path=chunk.heading_path,
        risk_level=chunk.risk_level,
        answerable_intents=chunk.answerable_intents,
        retrieval_source="bm25_rerank",
        metadata={
            **chunk.metadata,
            "bm25_score": bm25_score,
            "rewritten_query": query,
            "source_type": chunk.source_type,
            "chunk_index": chunk.chunk_index,
            "embedding_provider": chunk.embedding_provider,
            "embedding_model": chunk.embedding_model,
            "embedding_dimension": chunk.embedding_dimension,
            "embedding_distance": chunk.embedding_distance,
            "embedding_version": chunk.embedding_version,
        },
    )


def _default_chunks() -> list[KnowledgeChunk]:
    """构造带业务范围的示例知识，用于第一阶段离线检索基线。"""
    data: list[dict[str, Any]] = [
        {
            "doc_name": "Refund Arrival Policy",
            "collection": "refund_policy",
            "business_scope": "refund",
            "heading_path": ["售后政策", "退款到账"],
            "paragraph": "退款审核通过后通常 1-7 个工作日原路退回，具体到账时间以支付渠道处理为准。",
            "answerable_intents": ["refund", "consult", "other"],
            "risk_level": "medium",
        },
        {
            "doc_name": "Return Exchange Policy",
            "collection": "refund_policy",
            "business_scope": "return_goods",
            "metadata": {"category": "return_policy", "aliases": ["refund"]},
            "heading_path": ["售后政策", "退货条件"],
            "paragraph": "签收后 7 天内且商品不影响二次销售时，可申请退货；是否通过以售后审核结果为准。",
            "answerable_intents": ["refund", "consult", "other"],
            "risk_level": "medium",
        },
        {
            "doc_name": "Exchange Policy",
            "collection": "exchange_policy",
            "business_scope": "exchange",
            "heading_path": ["售后政策", "换货条件"],
            "paragraph": "商品存在质量问题、型号不符或配件缺失时，可提交换货申请，客服会结合订单状态和售后记录核实。",
            "answerable_intents": ["exchange", "consult", "other"],
            "risk_level": "medium",
        },
        {
            "doc_name": "Repair Policy",
            "collection": "repair_policy",
            "business_scope": "repair",
            "heading_path": ["维修政策", "处理周期"],
            "paragraph": "维修问题通常需要先建单核实故障现象，常规处理周期为 3-7 个工作日。",
            "answerable_intents": ["repair", "consult", "other"],
            "risk_level": "medium",
        },
        {
            "doc_name": "Logistics FAQ",
            "collection": "logistics_policy",
            "business_scope": "logistics",
            "heading_path": ["物流政策", "物流查询"],
            "paragraph": "订单发货后可通过订单号查询物流状态；未签收订单可优先核对配送进度。",
            "answerable_intents": ["logistics", "consult", "other"],
            "risk_level": "low",
        },
        {
            "doc_name": "Complaint Handling Script",
            "collection": "complaint_policy",
            "business_scope": "complaint",
            "heading_path": ["投诉处理", "人工审核"],
            "paragraph": "投诉、举报、赔付和强烈不满场景必须转人工处理，并保留完整会话记录。AI 不得承诺赔偿金额、处罚商家或法律结论。",
            "answerable_intents": ["complaint", "consult", "other"],
            "risk_level": "high",
        },
        {
            "doc_name": "Invoice FAQ",
            "collection": "invoice_policy",
            "business_scope": "invoice",
            "heading_path": ["发票政策", "开票信息"],
            "paragraph": "发票可在订单完成后申请开具；如需企业抬头，请提供准确抬头和税号。发票申请、修改或补开需要进入受控流程处理。",
            "answerable_intents": ["invoice", "consult", "other"],
            "risk_level": "low",
        },
        {
            "doc_name": "Member FAQ",
            "collection": "member_policy",
            "business_scope": "member",
            "heading_path": ["会员政策", "会员权益"],
            "paragraph": "会员权益包含专属客服、部分活动优先参与和售后进度提醒；具体权益以当前账号等级和活动规则为准。",
            "answerable_intents": ["member", "consult", "other"],
            "risk_level": "low",
        },
    ]
    return [
        KnowledgeChunk(
            version="V1.0",
            status="PUBLISHED",
            source_type="official_policy",
            chunk_index=index,
            **item,
        )
        for index, item in enumerate(data)
    ]


def _is_published_now(chunk: KnowledgeChunk) -> bool:
    """只允许已发布且未过期的 chunk 进入检索。"""
    now = datetime.utcnow()
    if chunk.status != "PUBLISHED":
        return False
    if chunk.effective_time and chunk.effective_time > now:
        return False
    if chunk.expire_time and chunk.expire_time <= now:
        return False
    return True


def _tokenize(text: str) -> list[str]:
    """中英文混合的轻量 token 化，作为 BM25 baseline。"""
    lowered = text.lower()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fa5]{2,}", lowered)
    chinese_chars = re.findall(r"[\u4e00-\u9fa5]", lowered)
    return words + chinese_chars


def _normalize_score(score: float) -> float:
    """把 rerank 分数归一化为 0-1 相关性分，避免暴露原始 BM25 含义。"""
    return round(score / (score + 3.0), 4) if score > 0 else 0.0
