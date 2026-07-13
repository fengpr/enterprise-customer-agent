import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from rag.document_loader import DocumentLoader
from rag.pg_vector_store import PgVectorStore
from rag.text_processing import KnowledgeChunk, split_into_chunks
from rag.vector_store import InMemoryVectorStore
from rag.quality import AnswerJudge, build_llm_judge_from_env, build_rag_trace, ensure_citation_ids, lexical_similarity
from schemas.intent_schema import Citation


SCOPE_CONFIG: dict[str, dict[str, Any]] = {
    "refund": {
        "collection": "refund_policy",
        "business_scope": "refund",
        "risk_level": "medium",
        "answerable_intents": ["refund", "consult", "other"],
    },
    "exchange": {
        "collection": "exchange_policy",
        "business_scope": "exchange",
        "risk_level": "medium",
        "answerable_intents": ["exchange", "consult", "other"],
    },
    "logistics": {
        "collection": "logistics_policy",
        "business_scope": "logistics",
        "risk_level": "low",
        "answerable_intents": ["logistics", "consult", "other"],
    },
    "invoice": {
        "collection": "invoice_policy",
        "business_scope": "invoice",
        "risk_level": "low",
        "answerable_intents": ["invoice", "consult", "other"],
    },
    "repair": {
        "collection": "repair_policy",
        "business_scope": "repair",
        "risk_level": "medium",
        "answerable_intents": ["repair", "consult", "other"],
    },
    "member": {
        "collection": "member_policy",
        "business_scope": "member",
        "risk_level": "low",
        "answerable_intents": ["member", "consult", "other"],
    },
    "complaint": {
        "collection": "complaint_policy",
        "business_scope": "complaint",
        "risk_level": "high",
        "answerable_intents": ["complaint", "consult", "other"],
    },
    "general": {
        "collection": "general_faq",
        "business_scope": "general",
        "risk_level": "low",
        "answerable_intents": ["consult", "other"],
    },
}

# 同一目录下可能包含多个细分业务文档，不能只按目录名赋予 metadata。
# 退货条件与退款到账同属 refund_policy 集合，但分别服务退货规则和退款进度检索。
DOCUMENT_CONFIG_OVERRIDES: dict[str, dict[str, Any]] = {
    "return_goods_policy": {
        "collection": "refund_policy",
        "business_scope": "return_goods",
        "risk_level": "medium",
        "answerable_intents": ["refund", "consult", "other"],
        # 保留与退款大类的关联，供“售后/退款”这类宽泛问法兜底召回。
        "metadata": {"aliases": ["refund"]},
    },
}


@dataclass
class EvalSample:
    """单条端到端 RAG 评估样本，检索字段兼容已有数据集。"""

    query: str
    intent: str
    user_goal: str
    business_scope: str
    expected_collection: str
    expected_doc: str
    must_contain: list[str]
    expected_risk_level: str
    reference_answer: str = ""
    # 单个字符串表示必须出现；字符串列表表示同一事实允许任一等价表述。
    required_facts: list[str | list[str]] | None = None
    forbidden_claims: list[str] | None = None
    expected_refusal: bool | None = None
    expected_tools: list[dict[str, Any]] | None = None


@dataclass
class GeneratedAnswer:
    """生成层输出，支持携带真实 Agent 的实际引用、决策信息与可选成本。"""

    answer: str
    citations: list[Citation] | None = None
    metadata: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] | None = None


def main() -> None:
    """命令行入口：运行离线 RAG 检索评估并输出 JSON 报告。"""
    parser = argparse.ArgumentParser(description="Evaluate RAG retrieval against JSONL samples.")
    parser.add_argument("--eval-dir", default="data/rag_eval")
    parser.add_argument("--kb-dir", default="data/kb_sources")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--backend", choices=["memory", "pgvector"], default="memory")
    parser.add_argument("--llm-judge", action="store_true", help="使用配置的模型执行生成阶段质量裁判")
    parser.add_argument("--generator", choices=["baseline", "agent"], default="baseline", help="选择证据基线或真实 CustomerServiceAgent")
    parser.add_argument("--deepeval", action="store_true", help="使用 DeepEval 评测黄金样本")
    parser.add_argument("--workers", type=int, default=int(__import__('os').getenv("RAG_GOLDEN_EVAL_CONCURRENCY", "3")), help="黄金样本并发数")
    args = parser.parse_args()

    judge = build_llm_judge_from_env() if args.llm_judge else None
    report = evaluate(eval_dir=args.eval_dir, kb_dir=args.kb_dir, top_k=args.top_k, backend=args.backend, judge=judge, generation_mode=args.generator, use_deepeval=args.deepeval, max_workers=args.workers)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def evaluate(
    eval_dir: str,
    kb_dir: str,
    top_k: int = 5,
    backend: str = "memory",
    answer_generator: Callable[[EvalSample, list[Citation]], str] | None = None,
    judge: AnswerJudge | None = None,
    generation_mode: str = "baseline",
    max_samples: int | None = None,
    use_deepeval: bool = False,
    max_workers: int = 1,
) -> dict[str, Any]:
    """执行端到端评估，覆盖召回、回答证据覆盖与可选 LLM 质量裁判。"""
    samples = load_eval_samples(eval_dir)
    if max_samples is not None:
        samples = samples[:max(1, max_samples)]
    store = _build_eval_store(backend, kb_dir)
    def run_sample(sample: EvalSample) -> dict[str, Any]:
        # 并行真实 Agent 时每条样本使用独立生成器，避免共享 LangGraph/LLM 客户端状态。
        generator = answer_generator or _generator_for_mode(generation_mode)
        results = store.similarity_search(
            sample.query,
            intent=sample.intent,
            user_goal=sample.user_goal,
            business_scope=sample.business_scope,
            top_k=top_k,
        )
        ensure_citation_ids(results)
        started_at = time.perf_counter()
        generated = generator(sample, results)
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        if isinstance(generated, GeneratedAnswer):
            answer = generated.answer
            answer_citations = generated.citations or results
            generation_metadata = generated.metadata or {}
            tool_results = generated.tool_results or []
        else:
            answer = generated
            answer_citations = results
            generation_metadata = {}
            tool_results = []
        ensure_citation_ids(answer_citations)
        row = _score_sample(sample, results, answer, answer_citations, judge, latency_ms, generation_metadata, retrieval_only=generation_mode == "baseline")
        if use_deepeval:
            try:
                from rag.deepeval_adapter import evaluate_golden_case
                deepeval_result = evaluate_golden_case({
                    "input": sample.query, "actual_output": answer, "expected_output": sample.reference_answer,
                    "retrieval_context": [item.paragraph for item in answer_citations],
                    "tools_called": [{"name": item.get("query_type") or item.get("tool_name"), "input_parameters": {}} for item in tool_results],
                    "expected_tools": sample.expected_tools or [],
                    # 投诉/赔付规则属于多触发条件的聚合策略，相关性用于监控而非阻断安全转人工。
                    "contextual_relevancy_threshold": 0.6 if sample.expected_risk_level in {"high", "critical"} else 0.7,
                })
                row["deepeval"] = deepeval_result
                row["failures"].extend(deepeval_result.get("failures", []))
            except Exception as exc:
                # Judge 属于可观测性依赖，超时或限流不能抹掉已完成的真实 Agent 评测结果。
                # 技术异常单独保存，后续可由后台重跑 Judge，不计为回答质量失败。
                row["deepeval"] = {"metrics": {}, "reasons": {}, "failures": [], "error": str(exc)}
                row["deepeval_error"] = str(exc)
        return row

    workers = max(1, max_workers)
    if workers == 1 or len(samples) <= 1:
        rows = [run_sample(sample) for sample in samples]
    else:
        rows_by_index: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="golden-eval") as executor:
            futures = {executor.submit(run_sample, sample): index for index, sample in enumerate(samples)}
            for future in as_completed(futures):
                rows_by_index[futures[future]] = future.result()
        rows = [rows_by_index[index] for index in range(len(samples))]
    return _build_report(rows, evaluation_mode=generation_mode)


def _build_eval_store(backend: str, kb_dir: str):
    """根据评估参数创建检索 store，memory 后端使用 kb_sources 真实样本文档。"""
    if backend == "memory":
        return InMemoryVectorStore(load_kb_chunks(kb_dir))
    if backend == "pgvector":
        return PgVectorStore()
    raise ValueError(f"Unsupported RAG eval backend: {backend}")


def load_eval_samples(eval_dir: str) -> list[EvalSample]:
    """读取 JSONL 评估集。"""
    samples: list[EvalSample] = []
    for file_path in sorted(Path(eval_dir).glob("*.jsonl")):
        for line_no, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                samples.append(EvalSample(**payload))
            except Exception as exc:
                raise ValueError(f"评估样本格式错误: {file_path}:{line_no}: {exc}") from exc
    return samples


def load_kb_chunks(kb_dir: str) -> list[KnowledgeChunk]:
    """从 kb_sources 目录加载知识样本文档并生成评估 chunks。"""
    root = Path(kb_dir)
    loader = DocumentLoader()
    chunks: list[KnowledgeChunk] = []
    for scope_dir in sorted(item for item in root.iterdir() if item.is_dir()):
        config = SCOPE_CONFIG.get(scope_dir.name)
        if not config:
            continue
        for file_path in sorted(scope_dir.rglob("*")):
            if file_path.suffix.lower() not in {".md", ".txt"}:
                continue
            # 目录配置提供默认值，文档级覆盖解决同一集合内的细分业务范围。
            document_config = {**config, **DOCUMENT_CONFIG_OVERRIDES.get(file_path.stem, {})}
            text = loader.load_text(str(file_path))
            document_chunks = split_into_chunks(
                text,
                doc_name=file_path.stem,
                version="V1.0",
                collection=document_config["collection"],
                business_scope=document_config["business_scope"],
                risk_level=document_config["risk_level"],
                answerable_intents=document_config["answerable_intents"],
                source_type="official_policy",
            )
            # aliases 等非结构化 metadata 也要随 chunk 写入，保证 memory/pgvector 行为一致。
            for chunk in document_chunks:
                chunk.metadata = dict(document_config.get("metadata", {}))
            chunks.extend(document_chunks)
    return chunks


def _find_missing_required_facts(required_facts: list[str | list[str]], answer: str) -> list[str]:
    """校验必需事实；同义表述组命中任一项即可通过，避免把自然表达误判为漏答。"""
    missing: list[str] = []
    for fact in required_facts:
        alternatives = fact if isinstance(fact, list) else [fact]
        if not any(item in answer for item in alternatives):
            # 失败诊断展示完整的可接受表达，方便人工定位而非只显示内部结构。
            missing.append(" / ".join(alternatives))
    return missing


def _score_sample(
    sample: EvalSample,
    results: list[Citation],
    answer: str,
    answer_citations: list[Citation],
    judge: AnswerJudge | None = None,
    generation_latency_ms: float = 0.0,
    generation_metadata: dict[str, Any] | None = None,
    retrieval_only: bool = False,
) -> dict[str, Any]:
    """计算单条端到端样本的检索、引用和生成质量。"""
    top1 = results[0] if results else None
    top3 = results[:3]
    expected_doc_hit = any(item.doc_name == sample.expected_doc for item in results)
    top1_doc_hit = bool(top1 and top1.doc_name == sample.expected_doc)
    top3_doc_hit = any(item.doc_name == sample.expected_doc for item in top3)
    collection_hit = bool(top1 and top1.collection == sample.expected_collection)
    scope_hit = bool(top1 and top1.business_scope == sample.business_scope)
    risk_hit = bool(top1 and top1.risk_level == sample.expected_risk_level)
    combined_text = "\n".join(item.paragraph for item in results[:3])
    must_contain_hit = all(keyword in combined_text for keyword in sample.must_contain)
    failures: list[str] = []
    if not results:
        failures.append("no_hit")
    if not top1_doc_hit:
        failures.append("top1_doc_mismatch")
    if not top3_doc_hit:
        failures.append("top3_doc_mismatch")
    if not collection_hit:
        failures.append("collection_mismatch")
    if not scope_hit:
        failures.append("scope_mismatch")
    if not risk_hit:
        failures.append("risk_mismatch")
    if not must_contain_hit and not retrieval_only:
        failures.append("must_contain_miss")
    trace = build_rag_trace(
        sample.query,
        answer_citations,
        answer,
        intent=sample.intent,
        user_goal=sample.user_goal,
        business_scope=sample.business_scope,
    )
    validation = trace["citation_validation"]
    required_facts = sample.required_facts or sample.must_contain
    missing_required_facts = _find_missing_required_facts(required_facts, answer)
    forbidden_claims = [fact for fact in (sample.forbidden_claims or []) if fact in answer]
    if validation["hallucination_detected"] and not retrieval_only:
        failures.append("unsupported_claim")
    if missing_required_facts and not retrieval_only:
        failures.append("missing_required_fact")
    if forbidden_claims and not retrieval_only:
        failures.append("forbidden_claim")
    judge_result = judge({
        "query": sample.query,
        "reference_answer": sample.reference_answer,
        "required_facts": required_facts,
        "forbidden_claims": sample.forbidden_claims or [],
        "answer": answer,
        "citations": [item.model_dump() for item in results],
    }) if judge else None
    if judge_result and judge_result.get("hallucination"):
        failures.append("llm_judge_hallucination")
    relevant_contexts = [item for item in results if item.doc_name == sample.expected_doc]
    context_precision = round(len(relevant_contexts) / len(results), 4) if results else 0.0
    context_recall = 1.0 if relevant_contexts else 0.0
    answer_relevance = lexical_similarity(sample.query, answer)
    semantic_similarity = lexical_similarity(sample.reference_answer, answer) if sample.reference_answer else float(not missing_required_facts)
    answer_correctness = float(not missing_required_facts and not forbidden_claims and not validation["hallucination_detected"])
    if judge_result:
        answer_correctness = round(float(judge_result.get("answer_correctness", 0)) / 5, 4)
        answer_relevance = round(float(judge_result.get("answer_relevance", judge_result.get("completeness", 0))) / 5, 4)
        semantic_similarity = round(float(judge_result.get("semantic_similarity", judge_result.get("answer_correctness", 0))) / 5, 4)
    actual_refusal = _is_refusal(answer)
    refusal_correct = None if sample.expected_refusal is None else actual_refusal == sample.expected_refusal

    return {
        "sample": asdict(sample),
        "top1_doc_hit": top1_doc_hit,
        "top3_doc_hit": top3_doc_hit,
        "expected_doc_hit": expected_doc_hit,
        "collection_hit": collection_hit,
        "scope_hit": scope_hit,
        "risk_hit": risk_hit,
        "must_contain_hit": must_contain_hit,
        "no_hit": not results,
        "failures": failures,
        "generated_answer": answer,
        "citation_validation": validation,
        "missing_required_facts": missing_required_facts,
        "required_facts_complete": not missing_required_facts,
        "forbidden_claims": forbidden_claims,
        "llm_judge": judge_result,
        "trace": trace,
        "context_precision": context_precision,
        "context_recall": context_recall,
        "faithfulness": validation["groundedness"],
        "answer_relevance": answer_relevance,
        "answer_correctness": answer_correctness,
        "semantic_similarity": semantic_similarity,
        "generation_latency_ms": generation_latency_ms,
        "estimated_cost": (generation_metadata or {}).get("estimated_cost"),
        "refusal_correct": refusal_correct,
        "actual": [
            {
                "doc_name": item.doc_name,
                "collection": item.collection,
                "business_scope": item.business_scope,
                "risk_level": item.risk_level,
                "score": item.score,
                "heading_path": item.heading_path,
                "paragraph": item.paragraph[:160],
            }
            for item in results[:3]
        ],
    }


def _build_report(rows: list[dict[str, Any]], evaluation_mode: str = "agent") -> dict[str, Any]:
    """汇总评估指标，默认只报告不失败。"""
    total = len(rows)
    failed = [row for row in rows if row["failures"]]
    metrics = {
            "total": total,
            "hit@1": _ratio(rows, "top1_doc_hit"),
            "hit@3": _ratio(rows, "top3_doc_hit"),
            "collection_accuracy": _ratio(rows, "collection_hit"),
            "doc_hit_rate": _ratio(rows, "expected_doc_hit"),
            "scope_accuracy": _ratio(rows, "scope_hit"),
            "risk_accuracy": _ratio(rows, "risk_hit"),
            "must_contain_hit_rate": _ratio(rows, "must_contain_hit"),
            "no_hit_count": sum(1 for row in rows if row["no_hit"]),
            "threshold_no_hit_count": sum(1 for row in rows if row["no_hit"] and "no_hit" in row["failures"]),
            "failed_count": len(failed),
            "answer_groundedness": _average(rows, "citation_validation", "groundedness"),
            "citation_precision": _average(rows, "citation_validation", "citation_precision"),
            "citation_recall": _average(rows, "citation_validation", "citation_recall"),
            "hallucination_count": sum(1 for row in rows if row["citation_validation"]["hallucination_detected"]),
            "required_fact_coverage": _ratio(rows, "required_facts_complete"),
            "llm_judged_count": sum(1 for row in rows if row["llm_judge"] is not None),
            "context_precision": _average_flat(rows, "context_precision"),
            "context_recall": _average_flat(rows, "context_recall"),
            "faithfulness": _average_flat(rows, "faithfulness"),
            "answer_relevance": _average_flat(rows, "answer_relevance"),
            "answer_correctness": _average_flat(rows, "answer_correctness"),
            "semantic_similarity": _average_flat(rows, "semantic_similarity"),
            "avg_generation_latency_ms": _average_flat(rows, "generation_latency_ms"),
            "refusal_accuracy": _optional_ratio(rows, "refusal_correct"),
            "estimated_cost_total": _known_cost_total(rows),
            "cost_measured_count": sum(1 for row in rows if row["estimated_cost"] is not None),
            "deepeval_judged_count": sum(1 for row in rows if row.get("deepeval")),
            "deepeval_error_count": sum(1 for row in rows if row.get("deepeval_error")),
        }
    if evaluation_mode == "baseline":
        # 基线只验证检索，不将“首条片段拼接文本”伪装成真实 Agent 生成质量。
        for key in (
            "answer_groundedness", "citation_precision", "citation_recall", "hallucination_count",
            "required_fact_coverage", "faithfulness", "answer_relevance", "answer_correctness",
            "semantic_similarity", "avg_generation_latency_ms", "estimated_cost_total",
            "cost_measured_count", "deepeval_judged_count",
        ):
            metrics[key] = None
    return {
        "evaluation_mode": evaluation_mode,
        "metrics": metrics,
        "failures": failed,
    }


def _ratio(rows: list[dict[str, Any]], key: str) -> float:
    """计算布尔指标比例。"""
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if row[key]) / len(rows), 4)


def _average(rows: list[dict[str, Any]], parent: str, key: str) -> float:
    """计算嵌套数值指标的平均值。"""
    if not rows:
        return 0.0
    return round(sum(float(row[parent][key]) for row in rows) / len(rows), 4)


def _average_flat(rows: list[dict[str, Any]], key: str) -> float:
    """计算扁平数值指标的平均值。"""
    return round(sum(float(row[key]) for row in rows) / len(rows), 4) if rows else 0.0


def _optional_ratio(rows: list[dict[str, Any]], key: str) -> float | None:
    """仅在样本声明了预期拒答行为时，统计拒答准确率。"""
    values = [row[key] for row in rows if row[key] is not None]
    return round(sum(1 for value in values if value) / len(values), 4) if values else None


def _known_cost_total(rows: list[dict[str, Any]]) -> float:
    """汇总生成器上报的估算成本；当前 Agent 未返回 token usage 时保持为 0。"""
    return round(sum(float(row["estimated_cost"]) for row in rows if row["estimated_cost"] is not None), 6)


def _grounded_baseline_answer(sample: EvalSample, citations: list[Citation]) -> str:
    """无生成模型时使用带引用的证据基线，保证离线评测仍能验证完整链路。"""
    if not citations:
        return "当前知识库未找到明确规则，暂无法确认具体处理条件。"
    citation = citations[0]
    return f"{citation.paragraph}【来源：{citation.citation_id}】"


def _generator_for_mode(mode: str) -> Callable[[EvalSample, list[Citation]], str | GeneratedAnswer]:
    """选择生成器；真实 Agent 模式复用线上 Agent，而不是复制一套 Prompt。"""
    if mode == "baseline":
        return _grounded_baseline_answer
    if mode == "agent":
        return _build_real_agent_generator()
    raise ValueError(f"Unsupported generation mode: {mode}")


def _build_real_agent_generator() -> Callable[[EvalSample, list[Citation]], GeneratedAnswer]:
    """调用真实 CustomerServiceAgent，保留它实际生成的回答、引用和路由结果。"""
    from agents.customer_service_agent import CustomerServiceAgent
    from schemas.intent_schema import AgentReplyRequest

    agent = CustomerServiceAgent()

    def generate(sample: EvalSample, _: list[Citation]) -> GeneratedAnswer:
        """复用同一个 Agent 实例，避免每条样本重复初始化检索链。"""
        reply = agent.reply(AgentReplyRequest(message=sample.query))
        return GeneratedAnswer(
            answer=reply.customer_message or reply.answer,
            citations=reply.citations,
            metadata={"decision_type": reply.decision_type, "estimated_cost": None},
            tool_results=reply.tool_results,
        )

    return generate


def _is_refusal(answer: str) -> bool:
    """识别安全拒答或受控转人工话术，供带 expected_refusal 的评测样本统计。"""
    return any(term in answer for term in ("无法确认", "暂无法", "不支持", "转人工", "人工处理", "受控流程"))


if __name__ == "__main__":
    main()
