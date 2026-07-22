"""独立启动会话滚动摘要 Worker。"""

from services.conversation_summary_service import run_worker


if __name__ == "__main__":
    run_worker()
