"""验证通用会话状态、短回复绑定和上下文压缩。"""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from repositories.conversation_state_repository import ConversationStateRepository
from services.conversation_state_service import ConversationStateService, SHANGHAI


def _service(tmp: str) -> ConversationStateService:
    return ConversationStateService(ConversationStateRepository(str(Path(tmp) / "state.db")))


def test_confirmation_restores_conditional_delivery_goal() -> None:
    """“这个订单→确认”必须恢复原条件诉求，而不是落入泛化问候。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        now = datetime(2026, 7, 19, 13, 0, tzinfo=SHANGHAI)

        first = service.resolve_turn(
            session_no="S1", message="明天收不到就帮我退了吧", selected_order_no=None, now=now
        )
        assert "选择" in (first.answer or "")
        second = service.resolve_turn(
            session_no="S1", message="这个订单", selected_order_no="EC202607160008", now=now
        )
        assert "请确认" in (second.answer or "")
        third = service.resolve_turn(
            session_no="S1", message="确认", selected_order_no="EC202607160008", now=now
        )

        assert third.action == "schedule_delivery_recheck"
        assert third.order_no == "EC202607160008"
        assert third.scheduled_at.startswith("2026-07-20T20:00:00")


def test_expired_confirmation_cannot_resume_old_goal() -> None:
    """确认问题超过五分钟后，短回复不能继续执行旧目标。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        now = datetime(2026, 7, 19, 13, 0, tzinfo=SHANGHAI)
        service.resolve_turn(
            session_no="S2", message="明天收不到就退款", selected_order_no="EC1", now=now
        )

        result = service.resolve_turn(
            session_no="S2", message="是的", selected_order_no="EC1", now=now + timedelta(minutes=6)
        )

        assert result.action is None
        assert "已失效" in (result.answer or "")


def test_recent_turns_are_limited_to_six_rounds() -> None:
    """原始上下文只保留最近六轮，更早信息进入确定性摘要。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        for index in range(8):
            service.record_result(
                session_no="S3",
                user_message=f"问题{index}",
                answer=f"回答{index}",
                selected_order_no=None,
                pending_action=None,
                result={"analysis": {"intent": "consult"}},
            )

        state = service.load("S3")
        assert len(state["recent_turns"]) == 12
        assert state["recent_turns"][0]["content"] == "问题2"
        assert state["summary"]["topics"] == ["consult"]


def test_legacy_confirmation_text_rebuilds_pending_interaction() -> None:
    """升级前只有确认话术的真实会话，也能恢复条件诉求并绑定下一次确认。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        service.hydrate_recent_turns(
            "S4",
            [
                {"sender_type": "customer", "content": "明天收不到货就帮我退了吧", "created_at": "2026-07-19T18:00:00+08:00"},
                {"sender_type": "ai", "content": "我理解您的诉求。", "created_at": "2026-07-19T18:00:01+08:00"},
                {"sender_type": "customer", "content": "这个订单", "created_at": "2026-07-19T18:01:00+08:00"},
                {"sender_type": "ai", "content": "请确认您说的是订单 EC202607160009 吗？", "created_at": "2026-07-19T18:01:01+08:00"},
                {"sender_type": "customer", "content": "确认", "created_at": "2026-07-19T18:02:00+08:00"},
                {"sender_type": "ai", "content": "请问您需要确认什么信息？", "created_at": "2026-07-19T18:02:01+08:00"},
            ],
        )

        result = service.resolve_turn(
            session_no="S4",
            message="确认",
            selected_order_no="EC202607160009",
            now=datetime.now(SHANGHAI),
        )

        assert result.action == "schedule_delivery_recheck"
        assert result.order_no == "EC202607160009"
        assert result.scheduled_at.startswith("2026-07-20T20:00:00")
