"""RAG 评测后台任务管理，避免全量真实 Agent 评测阻塞 Web 请求。"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable


class RagEvaluationJobManager:
    """使用受限线程池串行/低并发执行评测，保存任务进度与最终报告。"""

    def __init__(self, runner: Callable[[], dict[str, Any]]) -> None:
        """初始化任务执行器；默认单任务运行，防止批量评测压垮模型和业务工具。"""
        workers = max(1, int(os.getenv("RAG_EVAL_MAX_WORKERS", "1")))
        self.runner = runner
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rag-evaluation")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def submit(self) -> dict[str, Any]:
        """提交全量真实 Agent 评测；任务进入后台后立即返回任务编号。"""
        job_id = f"rag-eval-{uuid.uuid4().hex[:12]}"
        job = {
            "job_id": job_id,
            "status": "QUEUED",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "report": None,
            "error": None,
        }
        with self.lock:
            self.jobs[job_id] = job
        self.executor.submit(self._run, job_id)
        return self.get(job_id) or job

    def get(self, job_id: str) -> dict[str, Any] | None:
        """读取任务快照，避免把内部共享对象暴露给接口线程。"""
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def _run(self, job_id: str) -> None:
        """在线程池中运行评测，并捕获模型、工具或数据异常。"""
        with self.lock:
            self.jobs[job_id].update({"status": "RUNNING", "started_at": _now()})
        try:
            report = self.runner()
            with self.lock:
                self.jobs[job_id].update({"status": "SUCCEEDED", "report": report, "finished_at": _now()})
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update({"status": "FAILED", "error": str(exc), "finished_at": _now()})


def _now() -> str:
    """生成统一 UTC 时间，便于前端展示和后续接入持久化任务表。"""
    return datetime.now(timezone.utc).isoformat()
