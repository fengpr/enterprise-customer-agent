"""SSE 事件缓冲与断线续传测试。"""

from services.stream_event_service import StreamEventService


def test_event_contains_protocol_fields_and_replays_only_missing_events():
    """事件必须具备统一字段，携带 Last-Event-ID 时不得重复 delta。"""
    service = StreamEventService(redis_client=None)
    service._memory_events.clear()
    accepted = service.publish("req-stream-1", "accepted", {"status": "accepted"})
    delta = service.publish("req-stream-1", "delta", {"text": "您好"})

    assert {"request_id", "event_id", "event_type", "timestamp", "payload"} <= set(delta)
    assert service.replay("req-stream-1", accepted["event_id"]) == [delta]
    assert "event: delta" in service.to_sse(delta)


def test_event_buffer_drops_sensitive_fields():
    """事件缓冲不得保存客户令牌、Prompt 或工具原始返回。"""
    service = StreamEventService(redis_client=None)
    service._memory_events.clear()
    event = service.publish("req-stream-2", "delta", {"text": "安全文本", "auth_token": "secret", "prompt": "hidden", "tool_results": [{"raw": 1}]})

    assert event["payload"] == {"text": "安全文本"}
