"""验证会话摘要任务的安全载荷、异步处理和模型上下文贯通。"""

import json
from typing import Any

from agents.customer_service_agent import CustomerServiceAgent
from services.conversation_summary_service import (
    ConversationSummaryJob,
    ConversationSummaryProcessor,
    ConversationSummaryQueue,
    LLMConversationSummarizer,
)


class _Redis:
    """摘要队列测试所需的最小 Redis Stream 替身。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.events: list[tuple[str, dict[str, str]]] = []

    def xgroup_create(self, *_args, **_kwargs):
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def xadd(self, stream, fields):
        self.events.append((stream, fields))
        return "1-0"


def test_summary_queue_payload_does_not_contain_conversation_or_authorization(monkeypatch) -> None:
    """摘要队列只能保存会话标识与游标，不复制聊天正文或客户 Token。"""
    monkeypatch.setenv("CONVERSATION_SUMMARY_ENABLED", "true")
    redis = _Redis()
    queue = ConversationSummaryQueue(redis_client=redis)

    assert queue.enqueue(ConversationSummaryJob("S100", 8, 2)) is True
    fields = redis.events[0][1]

    assert set(fields) == {"session_no", "source_version", "summary_cursor", "attempt"}
    assert "Authorization" not in json.dumps(fields)
    assert "Bearer" not in json.dumps(fields)


def test_summary_processor_updates_only_window_overflow() -> None:
    """第十六轮进入窗口后，Worker 只摘要被移出的第一轮。"""

    class Queue:
        def __init__(self) -> None:
            self.acked: list[str] = []

        def recover_pending(self):
            return None

        def claim(self):
            return "1-0", ConversationSummaryJob("S200", 5, 2)

        def ack(self, message_id):
            self.acked.append(message_id)

        def retry_or_dead_letter(self, *_args):
            raise AssertionError("摘要成功时不应重试")

    class States:
        def __init__(self) -> None:
            self.applied: dict[str, Any] | None = None

        def load(self, _session_no):
            return {
                "version": 5,
                "summary": {"summary_cursor": 2, "summary_applied_cursor": 0, "rolling_summary": ""},
            }

        def apply_rolling_summary(self, session_no, **kwargs):
            self.applied = {"session_no": session_no, **kwargs}
            return True

    class Messages:
        def list_by_session(self, _session_no):
            messages = []
            for index in range(16):
                messages.append({"sender_type": "customer", "content": f"问题{index}", "extra_data": {}})
                messages.append({"sender_type": "ai", "content": f"回答{index}", "extra_data": {}})
            return messages

    class Summarizer:
        def __init__(self) -> None:
            self.turns = []

        def summarize(self, _previous, turns):
            self.turns = turns
            return "用户曾咨询问题0。", ["已讨论问题0"]

    queue = Queue()
    states = States()
    summarizer = Summarizer()
    processor = ConversationSummaryProcessor(
        queue=queue,
        states=states,
        messages=Messages(),
        summarizer=summarizer,
    )

    assert processor.run_once() is True
    assert [item["content"] for item in summarizer.turns] == ["问题0", "回答0"]
    assert states.applied and states.applied["summary_cursor"] == 2
    assert queue.acked == ["1-0"]


def test_invalid_llm_summary_falls_back_without_exception() -> None:
    """摘要模型输出非 JSON 时返回 None，不影响在线确定性记忆。"""
    summarizer = LLMConversationSummarizer(invoke_summary=lambda _payload: "not-json")
    assert summarizer.summarize("", [{"role": "user", "content": "查询订单"}]) is None


def test_structured_memory_is_injected_into_intent_prompt(monkeypatch) -> None:
    """上一轮目标和查询动作必须进入意图识别输入，而不是只在回复阶段可见。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    agent = CustomerServiceAgent()
    prompt = agent._message_for_intent_analysis(
        "重新查询",
        {
            "safe_context_summary": "最近关联当前订单",
            "structured_memory": {
                "recent_turns": [{"role": "user", "content": "查询当前订单状态"}],
                "summary": {"last_user_goal": "status_query", "last_tool_action": "order_detail"},
            },
        },
    )

    assert "结构化会话记忆" in prompt
    assert "status_query" in prompt
    assert "查询当前订单状态" in prompt
