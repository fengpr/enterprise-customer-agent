"""Agent 执行取消控制，避免把取消当作普通失败重试。"""

from __future__ import annotations

from typing import Callable, Mapping, Any


class AgentExecutionCancelled(Exception):
    """表示当前 request_id 被客户主动停止，携带仅客户可见的部分结果。"""

    def __init__(self, result: Mapping[str, Any] | None = None) -> None:
        super().__init__("agent_execution_cancelled")
        self.result = dict(result or {})


class CancellationToken:
    """为单个请求提供合作式取消检查，不保存凭证或用户敏感字段。"""

    def __init__(self, request_id: str, checker: Callable[[], bool]) -> None:
        self.request_id = request_id
        self._checker = checker

    def is_cancelled(self) -> bool:
        """返回取消标记；基础设施短暂异常时不误取消正常请求。"""
        try:
            return bool(self._checker())
        except Exception:
            return False

    def check(self) -> None:
        """在图节点、重试与工具调用边界尽快停止后续动作。"""
        if self.is_cancelled():
            raise AgentExecutionCancelled()
