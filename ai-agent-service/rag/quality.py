"""RAG 回答质量校验，覆盖引用有效性、证据覆盖和可选 LLM 裁判。"""

import json
import os
import re
import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

from schemas.intent_schema import Citation


_MARKER_PATTERN = re.compile(r"【(?:来源|引用)[:：]?(kb-[a-f0-9]{12})】")
# 引用标记通常紧跟在句号后，必须与前一句一起切分，否则会误判为未引用。
# 先按句末标点切分整句，再从整句中提取一个或多个引用标记。
# 原先的可选句末匹配会把 kb 引用 ID 拆成独立“句子”，导致含引用的投诉回复没有断言。
_SENTENCE_PATTERN = re.compile(r"[^。！？!?；;]+(?:[。！？!?；;]|$)")
_FACTUAL_TERMS = (
    "可", "需", "必须", "审核", "退款", "退货", "到账", "工作日", "订单", "工单", "发票", "物流", "赔", "金额", "天",
    # 高风险客服场景同样是业务断言，必须进入引用校验。
    "投诉", "人工", "转交", "受理",
)
_OPERATIONAL_STATUS_TERMS = ("已记录", "已受理", "待分派", "进入受控流程", "专人跟进", "保持联系方式", "收到您的投诉")


def ensure_citation_ids(citations: list[Citation]) -> list[Citation]:
    """为所有后端返回的引用补齐稳定 ID，避免 pgvector 与内存后端行为不一致。"""
    for index, citation in enumerate(citations):
        if citation.citation_id:
            continue
        source = "|".join([citation.doc_name, citation.version, str(citation.metadata.get("chunk_index", index)), citation.paragraph])
        citation.citation_id = f"kb-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]}"
    return citations


@dataclass
class ClaimCheck:
    """单个事实性断言与其证据的校验结果。"""

    claim: str
    cited_ids: list[str]
    supported: bool
    reason: str


def evaluate_answer_groundedness(answer: str, citations: list[Citation]) -> dict[str, Any]:
    """验证回答中的事实性断言是否引用本轮召回片段并得到文本支持。

    规则校验不替代语义裁判，但能拦截伪造引用、未引用的政策结论和明显越界数字。
    """
    ensure_citation_ids(citations)
    available = {citation.citation_id: citation for citation in citations if citation.citation_id}
    claims: list[ClaimCheck] = []
    for sentence in _split_factual_claims(answer):
        cited_ids = _MARKER_PATTERN.findall(sentence)
        invalid_ids = [item for item in cited_ids if item not in available]
        evidence = [available[item].paragraph for item in cited_ids if item in available]
        supported = bool(evidence) and not invalid_ids and any(_has_lexical_support(sentence, item) for item in evidence)
        reason = "supported" if supported else (
            "invalid_citation" if invalid_ids else "missing_or_unrelated_citation"
        )
        claims.append(ClaimCheck(sentence, cited_ids, supported, reason))

    supported_count = sum(1 for item in claims if item.supported)
    cited_count = sum(1 for item in claims if item.cited_ids)
    unsupported = [asdict(item) for item in claims if not item.supported]
    return {
        "status": "not_applicable" if not claims else ("passed" if not unsupported else "failed"),
        "claim_count": len(claims),
        "supported_claim_count": supported_count,
        "groundedness": round(supported_count / len(claims), 4) if claims else 1.0,
        "citation_precision": round(supported_count / cited_count, 4) if cited_count else 0.0,
        "citation_recall": round(cited_count / len(claims), 4) if claims else 1.0,
        "hallucination_detected": bool(unsupported),
        "unsupported_claims": unsupported,
        "available_citation_ids": sorted(available),
    }


def build_rag_trace(query: str, citations: list[Citation], answer: str, **context: Any) -> dict[str, Any]:
    """记录可复现的 RAG Trace，供离线回归、人工抽检和线上审计使用。"""
    ensure_citation_ids(citations)
    return {
        "query": query,
        "context": context,
        "retrieved_citations": [item.model_dump() for item in citations],
        "answer": answer,
        "citation_validation": evaluate_answer_groundedness(answer, citations),
    }


class AnswerJudge(Protocol):
    """LLM 或人工裁判的统一接口，便于评测时替换实现。"""

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class LLMJudge:
    """将任意 LangChain 可调用模型包装为结构化 RAG 裁判。"""

    def __init__(self, invoke: Callable[[str], Any]) -> None:
        self.invoke = invoke

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        """根据固定 rubric 评估回答正确性、完整性和事实依据。"""
        prompt = (
            "你是严格的 RAG 质量裁判。仅基于提供的证据判断，不要补充外部常识。"
            "返回 JSON：answer_correctness、completeness、answer_relevance、semantic_similarity、groundedness、citation_entailment（均为 1-5），"
            "hallucination（布尔值）、unsupported_claims（数组）、missing_required_facts（数组）、reason。\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        result = self.invoke(prompt)
        content = getattr(result, "content", result)
        if isinstance(content, list):
            content = "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
        return _parse_judge_json(str(content))


def build_llm_judge_from_env() -> LLMJudge:
    """按环境变量创建 OpenAI 兼容裁判；未配置密钥时明确失败，避免静默假评测。"""
    api_key = os.getenv("RAG_JUDGE_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        raise ValueError("RAG_JUDGE_API_KEY、OPENAI_API_KEY 或 LLM_API_KEY is required")
    from langchain_openai import ChatOpenAI

    model = os.getenv("RAG_JUDGE_MODEL", os.getenv("LLM_MODEL", "gpt-4o-mini"))
    client = ChatOpenAI(model=model, api_key=api_key, temperature=0, timeout=float(os.getenv("RAG_JUDGE_TIMEOUT", "30")))
    return LLMJudge(client.invoke)


def _split_factual_claims(answer: str) -> list[str]:
    """跳过问候语，仅保留可能承载业务事实的句子。"""
    # 兼容“句号后标引用”和“句号前标引用”两种模型输出，统一成后者再按句切分。
    normalized = re.sub(
        r"([。！？!?；;])((?:【(?:来源|引用)[:：]?kb-[a-f0-9]{12}】)+)",
        r"\2\1",
        answer,
    )
    return [
        item.strip()
        for item in _SENTENCE_PATTERN.findall(normalized)
        if _is_rag_factual_claim(item)
    ]


def _is_rag_factual_claim(sentence: str) -> bool:
    """识别需要由知识引用支持的断言，排除仅反映本轮工单状态的系统话术。"""
    if not any(term in sentence for term in _FACTUAL_TERMS):
        return False
    # 有引用的句子始终参与校验，避免模型伪造或错配 citation ID。
    if _MARKER_PATTERN.search(sentence):
        return True
    # “已受理/待分派”等由系统状态和工单链路提供，不应被误当作知识库事实。
    if any(term in sentence for term in _OPERATIONAL_STATUS_TERMS):
        return False
    return True


def _has_lexical_support(claim: str, evidence: str) -> bool:
    """以连续中文词和数字交集做低成本证据支持判断。"""
    # 使用重叠二元词而不是整段中文串，避免“转交人工专员”与“必须转人工”
    # 因措辞不同而错误判为无证据。
    claim_without_markers = _MARKER_PATTERN.sub("", claim)
    claim_terms = _support_terms(claim_without_markers)
    evidence_terms = _support_terms(evidence)
    # 引用 ID 自带十六进制数字，必须在删除标记后再提取业务数字。
    claim_numbers = set(re.findall(r"\d+(?:-\d+)?", claim_without_markers))
    evidence_numbers = set(re.findall(r"\d+(?:-\d+)?", evidence))
    # 金额、时效等数字不能只因“退款/到账”词重合就被放行。
    if claim_numbers and not claim_numbers.issubset(evidence_numbers):
        return False
    return len(claim_terms & evidence_terms) >= 1


def _support_terms(text: str) -> set[str]:
    """提取引用支持判断所需的中文二元词与数字，兼容自然语言改写。"""
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    return {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))} | set(
        re.findall(r"\d+(?:-\d+)?", text)
    )


def _parse_judge_json(content: str) -> dict[str, Any]:
    """解析裁判 JSON，模型偶发使用 Markdown 代码块时也能兼容。"""
    cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM Judge 返回的不是 JSON: {content[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM Judge 必须返回 JSON 对象")
    return data


def lexical_similarity(left: str, right: str) -> float:
    """用中文双字词与英文词的 Jaccard 相似度提供无模型环境下的可复现代理指标。"""
    left_terms = _similarity_terms(left)
    right_terms = _similarity_terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return round(len(left_terms & right_terms) / len(left_terms | right_terms), 4)


def _similarity_terms(text: str) -> set[str]:
    """提取中英文评测词元，避免单个汉字造成相似度虚高。"""
    chinese = re.findall(r"[\u4e00-\u9fff]{2}", text)
    english = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    return set(chinese + english)
