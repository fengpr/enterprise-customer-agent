"""独立启动定时物流复核 Worker。"""

from services.scheduled_followup_service import run_worker


if __name__ == "__main__":
    run_worker()
