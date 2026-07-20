"""定时复核任务的 Redis Stream 调度、可靠消费与客户通知。"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from repositories.conversation_state_repository import ConversationStateRepository, FollowupNotificationRepository
from services.conversation_state_service import ConversationStateService
from services.downstream_identity import build_execution_identity, issue_execution_credential
from tools.order_tools import OrderTools

try:
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import RedisError, ResponseError, TimeoutError as RedisTimeoutError
except ImportError:  # pragma: no cover - 仅用于未安装 Redis 客户端的最小开发环境
    RedisError = RedisConnectionError = RedisTimeoutError = ResponseError = RuntimeError


class ScheduledFollowupQueue:
    """使用独立 Consumer Group 执行到期复核，不与在线 Agent 队列争用。"""

    # v2 与旧版 XAUTOCLAIM-only Worker 隔离，避免旧进程继续消费并崩溃。
    stream_key = "agent:followup:stream:v2"
    dead_letter_key = "agent:followup:dead-letter:v2"
    group_name = "agent-followup-workers-v2"
    worker_heartbeat_prefix = "agent:followup:worker:heartbeat:v2:"

    def __init__(self, redis_client: Any | None = None, consumer_name: str | None = None) -> None:
        self._redis = redis_client
        self.consumer_name = consumer_name or f"followup-{uuid.uuid4().hex[:8]}"
        self.visibility_timeout_ms = int(os.getenv("FOLLOWUP_VISIBILITY_TIMEOUT_MS", "30000"))
        self.worker_heartbeat_ttl = int(os.getenv("FOLLOWUP_WORKER_HEARTBEAT_TTL_SECONDS", "20"))
        self._supports_xautoclaim: bool | None = None
        if self._redis is None and os.getenv("REDIS_URL"):
            try:
                import redis

                self._redis = redis.Redis.from_url(
                    os.environ["REDIS_URL"], decode_responses=True, socket_connect_timeout=1, socket_timeout=5
                )
                self._redis.ping()
            except Exception:
                self._redis = None
        if self._redis:
            try:
                self._redis.xgroup_create(self.stream_key, self.group_name, id="0", mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    def enqueue(self, followup_id: str, attempt: int = 0) -> str:
        """队列只保存任务编号和次数，不保存 Token、订单详情或工具结果。"""
        if not self.enabled:
            raise RuntimeError("followup_redis_unavailable")
        return str(self._redis.xadd(self.stream_key, {"followup_id": followup_id, "attempt": str(attempt)}))

    def claim(self, block_ms: int = 1000) -> tuple[str, str, int] | None:
        if not self.enabled:
            return None
        rows = self._redis.xreadgroup(
            self.group_name, self.consumer_name, {self.stream_key: ">"}, count=1, block=block_ms
        )
        if not rows:
            return None
        message_id, fields = rows[0][1][0]
        return str(message_id), str(fields.get("followup_id")), int(fields.get("attempt") or 0)

    def recover_pending(self) -> tuple[str, str, int] | None:
        """接管超时 Pending；Redis 5 自动使用 XPENDING + XCLAIM 兼容路径。"""
        if not self.enabled:
            return None
        messages: list[Any] = []
        if self._supports_xautoclaim is not False:
            try:
                claimed = self._redis.xautoclaim(
                    self.stream_key,
                    self.group_name,
                    self.consumer_name,
                    self.visibility_timeout_ms,
                    "0-0",
                    count=1,
                )
                self._supports_xautoclaim = True
                messages = claimed[1] if claimed and len(claimed) > 1 else []
            except ResponseError as exc:
                # XAUTOCLAIM 从 Redis 6.2 才提供，旧服务端只在首次探测时返回 unknown command。
                if "unknown command" not in str(exc).lower() and "xautoclaim" not in str(exc).lower():
                    raise
                self._supports_xautoclaim = False
        if self._supports_xautoclaim is False:
            messages = self._recover_pending_legacy()
        if not messages:
            return None
        message_id, fields = messages[0]
        return str(message_id), str(fields.get("followup_id")), int(fields.get("attempt") or 0)

    def _recover_pending_legacy(self) -> list[Any]:
        """在 Redis 5 上使用扩展 XPENDING 找到超时消息，再交给当前消费者。"""
        pending = self._redis.xpending_range(self.stream_key, self.group_name, "-", "+", 10)
        expired_ids: list[str] = []
        for item in pending:
            message_id = item.get("message_id") if isinstance(item, dict) else item[0]
            idle = item.get("time_since_delivered", 0) if isinstance(item, dict) else item[2]
            if int(idle or 0) >= self.visibility_timeout_ms:
                expired_ids.append(str(message_id))
                break
        if not expired_ids:
            return []
        return list(
            self._redis.xclaim(
                self.stream_key,
                self.group_name,
                self.consumer_name,
                self.visibility_timeout_ms,
                expired_ids,
            )
            or []
        )

    def ack(self, message_id: str) -> None:
        if self.enabled:
            self._redis.xack(self.stream_key, self.group_name, message_id)

    def heartbeat(self) -> None:
        """发布当前协议 Worker 心跳，启动脚本不会再把旧进程误认为新 Worker。"""
        if not self.enabled:
            return
        try:
            self._redis.setex(
                f"{self.worker_heartbeat_prefix}{self.consumer_name}",
                self.worker_heartbeat_ttl,
                str(int(time.time())),
            )
        except RedisError:
            pass

    def has_active_worker(self) -> bool:
        """仅检查 v2 心跳，旧版复核 Worker 不会阻止新版本启动。"""
        if not self.enabled:
            return False
        try:
            return any(self._redis.scan_iter(match=f"{self.worker_heartbeat_prefix}*"))
        except RedisError:
            return False

    def dead_letter(self, message_id: str, followup_id: str, attempt: int, error_code: str) -> None:
        """死信仅保存安全错误码，不复制客户消息或业务响应。"""
        if not self.enabled:
            return
        self._redis.xadd(
            self.dead_letter_key,
            {"followup_id": followup_id, "attempt": str(attempt), "error_code": error_code[:64]},
        )
        self.ack(message_id)


class ScheduledFollowupProcessor:
    """扫描到期任务、查询最新物流并写回原会话和站内通知。"""

    def __init__(
        self,
        *,
        repository: FollowupNotificationRepository | None = None,
        queue: ScheduledFollowupQueue | None = None,
        order_tools: OrderTools | None = None,
        messages: ChatMessageRepository | None = None,
        state_service: ConversationStateService | None = None,
    ) -> None:
        self.repository = repository or FollowupNotificationRepository()
        self.queue = queue or ScheduledFollowupQueue()
        self.order_tools = order_tools or OrderTools()
        self.messages = messages or ChatMessageRepository(ChatSessionRepository())
        self.state_service = state_service or ConversationStateService(ConversationStateRepository())

    def dispatch_due(self, now: datetime | None = None) -> int:
        """将数据库中到期任务投递到独立 Stream；数据库始终保留权威状态。"""
        if not self.queue.enabled:
            return 0
        count = 0
        current = (now or datetime.now(UTC)).isoformat()
        for task in self.repository.list_due(current):
            try:
                self.queue.enqueue(str(task["followup_id"]), int(task.get("attempts") or 0))
                self.repository.update_status(str(task["followup_id"]), "QUEUED")
                count += 1
            except Exception:
                # 投递失败保留 PENDING，下一轮扫描可继续恢复。
                continue
        return count

    def run_once(self) -> bool:
        """消费一条复核任务，失败受控重试，超限进入独立 DLQ。"""
        claimed = self.queue.recover_pending() or self.queue.claim()
        if not claimed:
            return False
        message_id, followup_id, attempt = claimed
        task = self.repository.get_followup(followup_id)
        if not task or task.get("status") in {"CANCELLED", "COMPLETED"}:
            self.queue.ack(message_id)
            return True
        attempt += 1
        self.repository.update_status(followup_id, "RUNNING", attempts=attempt)
        try:
            self._execute(task)
            self.queue.ack(message_id)
        except Exception as exc:
            error_code = self._safe_error_code(exc)
            if attempt >= int(task.get("max_attempts") or 3):
                self.repository.update_status(followup_id, "FAILED", attempts=attempt, error_code=error_code)
                self.queue.dead_letter(message_id, followup_id, attempt, error_code)
            else:
                self.repository.update_status(followup_id, "PENDING", attempts=attempt, error_code=error_code)
                self.queue.ack(message_id)
        return True

    def _execute(self, task: dict[str, Any]) -> None:
        followup_id = str(task["followup_id"])
        customer_id = int(task["customer_id"])
        order_no = str(task["order_no"])
        request_id = f"followup-{followup_id}"
        credential = issue_execution_credential(customer_id, request_id)
        identity = build_execution_identity(customer_id, request_id, credential)

        # 到期时重新校验订单归属，再读取最新物流，绝不依赖旧摘要执行动作。
        order_result = self.order_tools.query_order(order_no, identity)
        if order_result.get("status") != "success":
            raise RuntimeError("order_ownership_validation_failed")
        logistics = self.order_tools.query_order_logistics(order_no, identity)
        if logistics.get("status") != "success":
            raise RuntimeError("logistics_query_failed")

        data = logistics.get("data") or {}
        status = str(data.get("statusLabel") or data.get("status") or data.get("logisticsStatus") or "状态已更新")
        message = (
            f"您设置的订单 {order_no} 物流复核已完成，当前物流状态为“{status}”。"
            "如果您仍未收到，请回复“确认”，我会继续为您处理物流异常或售后；系统不会自动创建退货工单。"
        )
        self.messages.save(
            session_no=str(task["session_no"]),
            sender_type="ai",
            sender_id="scheduled-followup",
            content=message,
            extra_data={
                "customer_message": message,
                "service_status": "物流复核已完成",
                "scheduled_followup_id": followup_id,
            },
        )
        self.repository.create_notification(
            customer_id=customer_id,
            session_no=str(task["session_no"]),
            followup_id=followup_id,
            title="物流复核结果",
            content=message,
        )
        self.state_service.await_delivery_receipt_confirmation(
            session_no=str(task["session_no"]), order_no=order_no, followup_id=followup_id
        )
        self.repository.update_status(
            followup_id,
            "COMPLETED",
            attempts=int(task.get("attempts") or 0) + 1,
            result_summary={"customer_message": message, "logistics_status": status},
        )

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        text = str(exc).lower()
        for code in ("order_ownership_validation_failed", "logistics_query_failed"):
            if code in text:
                return code
        return "followup_execution_failed"


def run_worker() -> None:
    """运行独立复核 Worker；空队列时周期扫描，进程重启后可恢复 Pending。"""
    if not os.getenv("REDIS_URL"):
        raise RuntimeError("定时复核 Worker 缺少 REDIS_URL 配置")
    processor = ScheduledFollowupProcessor()
    warned = False
    while not processor.queue.enabled:
        if not warned:
            print("定时复核 Worker 正在等待 Redis 就绪，将自动重连而不会退出。", flush=True)
            warned = True
        time.sleep(2)
        processor = ScheduledFollowupProcessor()
    if warned:
        print("Redis 已恢复，定时复核 Worker 开始消费任务。", flush=True)
    while True:
        try:
            processor.queue.heartbeat()
            processor.dispatch_due()
            if not processor.run_once():
                time.sleep(1)
        except (RedisConnectionError, RedisTimeoutError, RedisError):
            # Redis 重启或短暂抖动时保留 Worker 进程，数据库任务仍保持权威状态。
            time.sleep(2)
