"""验证定时复核的可靠载荷、会话写回和通知闭环。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from repositories.conversation_state_repository import ConversationStateRepository, FollowupNotificationRepository
from services.conversation_state_service import ConversationStateService
from services.scheduled_followup_service import ScheduledFollowupProcessor, ScheduledFollowupQueue
from redis.exceptions import ResponseError


class FakeRedis:
    """支持复核队列测试所需的最小 Redis Stream 行为。"""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.pending: list[tuple[str, dict[str, str]]] = []
        self.acked: list[str] = []

    def xgroup_create(self, *args, **kwargs): return True
    def xadd(self, stream, fields):
        message_id = f"{len(self.streams.get(stream, [])) + 1}-0"
        self.streams.setdefault(stream, []).append((message_id, fields))
        return message_id
    def xreadgroup(self, group, consumer, streams, count=1, block=0):
        stream = next(iter(streams))
        if not self.streams.get(stream): return []
        message = self.streams[stream].pop(0)
        self.pending = [message]
        return [(stream, [message])]
    def xautoclaim(self, *args, **kwargs): return ("0-0", [], [])
    def xack(self, stream, group, message_id): self.acked.append(message_id)


class FakeOrderTools:
    """只返回客户安全字段，不参与工单创建。"""

    def query_order(self, order_no, identity):
        assert "Bearer" not in identity
        return {"status": "success", "data": {"orderNo": order_no}}

    def query_order_logistics(self, order_no, identity):
        return {"status": "success", "data": {"statusLabel": "运输中"}}


class LegacyRedis(FakeRedis):
    """模拟支持 Stream、但尚未提供 XAUTOCLAIM 的 Redis 5。"""

    def __init__(self) -> None:
        super().__init__()
        self.xautoclaim_calls = 0
        self.pending = [("9-0", {"followup_id": "F9", "attempt": "1"})]

    def xautoclaim(self, *args, **kwargs):
        self.xautoclaim_calls += 1
        raise ResponseError("unknown command `XAUTOCLAIM`")

    def xpending_range(self, *args, **kwargs):
        return [{"message_id": "9-0", "time_since_delivered": 60_000}]

    def xclaim(self, *args, **kwargs):
        return self.pending


def test_queue_payload_contains_only_followup_identifier() -> None:
    redis = FakeRedis()
    queue = ScheduledFollowupQueue(redis_client=redis, consumer_name="test")
    queue.enqueue("F1")
    fields = redis.streams[queue.stream_key][0][1]
    assert fields == {"followup_id": "F1", "attempt": "0"}
    assert "Authorization" not in str(fields)


def test_redis_five_falls_back_to_pending_and_claim() -> None:
    """旧 Redis 不支持 XAUTOCLAIM 时 Worker 仍能恢复 Pending，且只探测一次。"""
    redis = LegacyRedis()
    queue = ScheduledFollowupQueue(redis_client=redis, consumer_name="legacy")

    assert queue.recover_pending() == ("9-0", "F9", 1)
    assert queue.recover_pending() == ("9-0", "F9", 1)
    assert redis.xautoclaim_calls == 1


def test_due_followup_writes_session_and_notification_without_creating_ticket() -> None:
    with TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "followup.db")
        sessions = ChatSessionRepository(db_path)
        session = sessions.create(7, "物流复核")
        messages = ChatMessageRepository(sessions)
        state_repository = ConversationStateRepository(db_path)
        repository = FollowupNotificationRepository(db_path)
        followup = repository.create_followup(
            session_no=session["session_id"],
            customer_id=7,
            order_no="EC100",
            scheduled_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            idempotency_key="delivery-recheck:7:S:EC100:T",
        )
        processor = ScheduledFollowupProcessor(
            repository=repository,
            queue=ScheduledFollowupQueue(redis_client=FakeRedis(), consumer_name="test"),
            order_tools=FakeOrderTools(),
            messages=messages,
            state_service=ConversationStateService(state_repository),
        )

        assert processor.dispatch_due() == 1
        assert processor.run_once() is True

        stored = repository.get_followup(followup["followup_id"])
        assert stored["status"] == "COMPLETED"
        assert repository.unread_count(7) == 1
        assert "不会自动创建退货工单" in messages.list_by_session(session["session_id"])[-1]["content"]
        state = state_repository.get(session["session_id"])
        assert state["pending_interaction"]["parent_goal"] == "delivery_not_received_check"
