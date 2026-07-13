"""独立评测 Worker 入口：从持久化队列批量领取 Trace 并调用 DeepEval。"""

import argparse
import time
from pathlib import Path

from rag.deepeval_adapter import evaluate_online_trace
from rag.evaluate import evaluate
from repositories.evaluation_repository import EvaluationRepository


def run_once(repository: EvaluationRepository, batch_size: int = 10) -> int:
    """处理一个批次；单线程执行以遵守 Judge 供应商限流并控制成本。"""
    job = repository.claim_job()
    if job:
        try:
            if job["job_type"] == "GOLDEN":
                base_dir = Path(__file__).resolve().parents[1] / "data"
                sample_limit = (job.get("payload") or {}).get("max_samples")
                workers = max(1, int(__import__("os").getenv("RAG_GOLDEN_EVAL_CONCURRENCY", "3")))
                report = evaluate(str(base_dir / "rag_eval"), str(base_dir / "kb_sources"), generation_mode="agent", use_deepeval=True, max_samples=sample_limit, max_workers=workers)
                repository.complete_job(job["job_id"], report=report)
            else:
                repository.complete_job(job["job_id"], error=f"Unsupported job_type: {job['job_type']}")
        except Exception as exc:
            repository.complete_job(job["job_id"], error=str(exc))
    traces = repository.claim_pending(batch_size)
    for item in traces:
        try:
            result = evaluate_online_trace(item["payload"])
            result["failures"] = list(result.get("failures", [])) + list(result.get("hard_rule_failures", []))
            repository.save_result(item["trace_id"], result)
        except Exception as exc:
            if item["attempts"] >= 3:
                repository.mark_failed(item["trace_id"], str(exc))
            else:
                # 重新进入队列；常驻 Worker 的轮询间隔避免立即重试压垮 Judge。
                repository.release_for_retry(item["trace_id"], str(exc), item["attempts"])
    return len(traces)


def main() -> None:
    """支持单次运行与常驻轮询，生产环境建议由独立进程或调度器启动。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    repository = EvaluationRepository()
    while True:
        run_once(repository)
        if args.once: return
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
