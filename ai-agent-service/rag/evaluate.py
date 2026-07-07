import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rag.document_loader import DocumentLoader
from rag.pg_vector_store import PgVectorStore
from rag.text_processing import KnowledgeChunk, split_into_chunks
from rag.vector_store import InMemoryVectorStore
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


@dataclass
class EvalSample:
    """单条 RAG 检索评估样本。"""

    query: str
    intent: str
    user_goal: str
    business_scope: str
    expected_collection: str
    expected_doc: str
    must_contain: list[str]
    expected_risk_level: str


def main() -> None:
    """命令行入口：运行离线 RAG 检索评估并输出 JSON 报告。"""
    parser = argparse.ArgumentParser(description="Evaluate RAG retrieval against JSONL samples.")
    parser.add_argument("--eval-dir", default="data/rag_eval")
    parser.add_argument("--kb-dir", default="data/kb_sources")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--backend", choices=["memory", "pgvector"], default="memory")
    args = parser.parse_args()

    report = evaluate(eval_dir=args.eval_dir, kb_dir=args.kb_dir, top_k=args.top_k, backend=args.backend)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def evaluate(eval_dir: str, kb_dir: str, top_k: int = 5, backend: str = "memory") -> dict[str, Any]:
    """执行 RAG 检索评估，返回指标和失败明细。"""
    samples = load_eval_samples(eval_dir)
    store = _build_eval_store(backend, kb_dir)
    rows: list[dict[str, Any]] = []
    for sample in samples:
        results = store.similarity_search(
            sample.query,
            intent=sample.intent,
            user_goal=sample.user_goal,
            business_scope=sample.business_scope,
            top_k=top_k,
        )
        rows.append(_score_sample(sample, results))
    return _build_report(rows)


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
            text = loader.load_text(str(file_path))
            chunks.extend(
                split_into_chunks(
                    text,
                    doc_name=file_path.stem,
                    version="V1.0",
                    collection=config["collection"],
                    business_scope=config["business_scope"],
                    risk_level=config["risk_level"],
                    answerable_intents=config["answerable_intents"],
                    source_type="official_policy",
                )
            )
    return chunks


def _score_sample(sample: EvalSample, results: list[Citation]) -> dict[str, Any]:
    """计算单条样本的命中情况。"""
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
    if not must_contain_hit:
        failures.append("must_contain_miss")

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


def _build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总评估指标，默认只报告不失败。"""
    total = len(rows)
    failed = [row for row in rows if row["failures"]]
    return {
        "metrics": {
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
        },
        "failures": failed,
    }


def _ratio(rows: list[dict[str, Any]], key: str) -> float:
    """计算布尔指标比例。"""
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if row[key]) / len(rows), 4)


if __name__ == "__main__":
    main()
