"""验证站内通知和复核任务接口的客户归属边界。"""

from fastapi.testclient import TestClient

import app


class FakeFollowups:
    def __init__(self) -> None:
        self.last_customer_id = None

    def list_notifications(self, customer_id, limit):
        self.last_customer_id = customer_id
        return [{"notification_id": "N1", "session_no": "S1", "notification_type": "DELIVERY_RECHECK", "title": "复核", "content": "结果", "is_read": 0, "created_at": "2026-07-19T12:00:00Z"}]

    def unread_count(self, customer_id):
        self.last_customer_id = customer_id
        return 1

    def mark_read(self, notification_id, customer_id):
        self.last_customer_id = customer_id
        return notification_id == "N1" and customer_id == 8

    def list_followups(self, customer_id, limit):
        self.last_customer_id = customer_id
        return []

    def cancel_followup(self, followup_id, customer_id):
        self.last_customer_id = customer_id
        return followup_id == "F1" and customer_id == 8


def test_notification_endpoints_use_authenticated_customer(monkeypatch) -> None:
    fake = FakeFollowups()
    monkeypatch.setattr(app, "followup_notifications", fake)
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"customer_id": 8, "role": "customer"})
    client = TestClient(app.app)

    response = client.get("/api/customer/notifications", headers={"Authorization": "Bearer redacted"})
    assert response.status_code == 200
    assert response.json()[0]["session_id"] == "S1"
    assert "customer_id" not in response.json()[0]
    assert fake.last_customer_id == 8
    assert client.get("/api/customer/notifications/unread-count").json() == {"count": 1}
    assert client.post("/api/customer/notifications/N1/read").status_code == 200


def test_customer_cannot_mutate_unknown_followup(monkeypatch) -> None:
    fake = FakeFollowups()
    monkeypatch.setattr(app, "followup_notifications", fake)
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"customer_id": 9, "role": "customer"})
    response = TestClient(app.app).post("/api/customer/follow-ups/F1/cancel")
    assert response.status_code == 409
    assert fake.last_customer_id == 9
