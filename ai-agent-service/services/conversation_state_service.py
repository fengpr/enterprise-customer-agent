"""通用会话状态归并、短回复绑定与条件性任务恢复。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from repositories.conversation_state_repository import ConversationStateConflict, ConversationStateRepository
from services.cache_service import CacheService


SHANGHAI = ZoneInfo("Asia/Shanghai")
CONFIRM_WORDS = {"是", "是的", "确认", "没错", "对", "对的", "继续", "可以"}
CANCEL_WORDS = {"取消", "算了", "不用了", "不需要了"}
RECENT_TURN_MESSAGE_LIMIT = 30
REPEAT_WORDS = {"重新查询", "再查一下", "再查询一下", "重新查一下", "刷新一下", "再看看", "重新执行查询"}
CONTINUE_WORDS = {"继续", "接着处理", "继续处理", "接着查"}


@dataclass(slots=True)
class TurnResolution:
    """表示本轮在调用 LLM 前得到的确定性处理结果。"""

    state: dict[str, Any]
    action: str | None = None
    answer: str | None = None
    order_no: str | None = None
    scheduled_at: str | None = None
    resumed_message: str | None = None


class ConversationStateService:
    """维护结构化会话状态，避免将“确认”等短回复交给模型猜测。"""

    def __init__(self, repository: ConversationStateRepository, cache: CacheService | None = None) -> None:
        self.repository = repository
        self.cache = cache or CacheService(namespace="conversation-state")

    @staticmethod
    def empty_state() -> dict[str, Any]:
        """创建不包含客户敏感信息的初始状态。"""
        return {
            "recent_turns": [],
            "summary": {
                "topics": [],
                "active_topic": None,
                "last_intent": None,
                "last_user_goal": None,
                "last_query": None,
                "last_tool_action": None,
                "last_result_status": None,
                "unfinished_goal": None,
                "unresolved_goal": None,
                "confirmed_facts": [],
                "rolling_summary": "",
                "summary_cursor": 0,
                "summary_applied_cursor": 0,
                "summary_version": 0,
            },
            "entity_context": {"order": None, "ticket": None, "product": None},
            "pending_interaction": None,
            "pending_action": None,
            "scheduled_followups": [],
            "version": 0,
        }

    def load(self, session_no: str) -> dict[str, Any]:
        """读取权威状态，并为历史会话补齐新增字段。"""
        cache_key = self.cache.key("session", session_no=session_no)
        stored = self.cache.get(cache_key, "session_cache_hit")
        if stored is None:
            stored = self.repository.get(session_no) or self.empty_state()
            self.cache.set(cache_key, stored, 30)
        base = self.empty_state()
        base.update(stored)
        base["summary"] = {**self.empty_state()["summary"], **(stored.get("summary") or {})}
        base["entity_context"] = {**self.empty_state()["entity_context"], **(stored.get("entity_context") or {})}
        return base

    def resolve_turn(
        self,
        *,
        session_no: str,
        message: str,
        selected_order_no: str | None,
        now: datetime | None = None,
    ) -> TurnResolution:
        """按优先级解析当前消息，优先绑定未完成确认，再处理新目标。"""
        current = now or datetime.now(SHANGHAI)
        state = self.load(session_no)
        pending = state.get("pending_interaction") or {}
        normalized = re.sub(r"[\s，。！？!?]", "", message or "")

        if pending and pending.get("status") == "WAITING":
            expires_at = self._parse_time(pending.get("expires_at"))
            if expires_at and current > expires_at:
                state["pending_interaction"] = None
                if normalized in CONFIRM_WORDS:
                    self.save(session_no, state)
                    # 物流复核是异步通知，客户可能隔天才打开会话；过期后应给出与通知场景匹配的下一步，不能让客户重新描述全部诉求。
                    if pending.get("parent_goal") == "delivery_not_received_check":
                        return TurnResolution(
                            state=state,
                            answer="这条物流复核结果已超过可确认时效。请重新查询该订单物流；如果仍未收到，可选择该订单后说明“仍未收到”，我会重新核验并继续协助您处理。",
                        )
                    return TurnResolution(state=state, answer="刚才的确认问题已失效，请重新说明您要处理的订单和诉求。")
            elif normalized in CANCEL_WORDS:
                state["pending_interaction"] = None
                self.save(session_no, state)
                return TurnResolution(state=state, answer="好的，已取消刚才等待确认的操作。")
            elif normalized in CONFIRM_WORDS:
                return self._confirm_pending(session_no, state, pending, selected_order_no, current)
            elif pending.get("interaction_type") == "select_order" and selected_order_no:
                return self._ask_order_confirmation(session_no, state, pending, selected_order_no, current)
            elif self._is_explicit_new_topic(message, str(pending.get("parent_goal") or "")):
                # 显式新业务目标优先于旧确认，避免用户查询物流时仍被拉回退货确认。
                state["pending_interaction"] = None
                self.save(session_no, state)

        operation = self._resolve_operation(normalized)
        if operation in {"repeat", "continue"} and not pending:
            repeated = self._restore_read_only_query(state, selected_order_no)
            if repeated:
                return repeated
            if operation == "repeat" and self._last_operation_was_write(state):
                return TurnResolution(
                    state=state,
                    answer="刚才涉及退货、退款或工单等业务操作，不能直接重复执行。请明确是否继续当前流程，我会先核对状态并避免重复提交。",
                )

        if self._is_delivery_contingency(message):
            scheduled_at = self._resolve_followup_time(message, current)
            resume_payload = {
                "goal": "conditional_delivery_after_sale",
                "original_message": message[:300],
                "scheduled_at": scheduled_at.isoformat(),
            }
            if selected_order_no:
                pending = self._pending(
                    "confirm_order",
                    selected_order_no,
                    "conditional_delivery_after_sale",
                    resume_payload,
                    current,
                )
                state["pending_interaction"] = pending
                self._set_order_context(state, selected_order_no, "selected_by_user", current, confirmed=False)
                self.save(session_no, state)
                return TurnResolution(
                    state=state,
                    answer=f"我理解您的意思是：如果届时仍未收到订单 {selected_order_no}，希望继续处理售后。请确认是这笔订单吗？",
                )
            state["pending_interaction"] = self._pending(
                "select_order", None, "conditional_delivery_after_sale", resume_payload, current
            )
            self.save(session_no, state)
            return TurnResolution(state=state, answer="可以为您设置到期物流复核。请先在上方选择要关注的订单，或提供订单号。")

        # “这个订单”只能绑定前端当前选择，不从长期历史猜测。
        if self._is_fuzzy_order_reference(message) and selected_order_no and pending:
            return self._ask_order_confirmation(session_no, state, pending, selected_order_no, current)
        return TurnResolution(state=state)

    def hydrate_recent_turns(self, session_no: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """为历史会话首次构建最近十五轮，之后只做增量压缩。"""
        state = self.load(session_no)
        if state.get("recent_turns"):
            return state
        turns: list[dict[str, Any]] = []
        for item in messages[-60:]:
            role = "user" if item.get("sender_type") == "customer" else "assistant" if item.get("sender_type") == "ai" else None
            if not role:
                continue
            extra = item.get("extra_data") or {}
            if extra.get("generation_cancelled"):
                continue
            turns.append(
                {
                    "role": role,
                    "content": self._safe_text(str(extra.get("customer_message") or item.get("content") or "")),
                    "at": str(item.get("created_at") or ""),
                }
            )
        state["recent_turns"] = turns[-RECENT_TURN_MESSAGE_LIMIT:]
        self._rebuild_pending_from_history(state, turns[-RECENT_TURN_MESSAGE_LIMIT:])
        return self.save(session_no, state) if turns else state

    def record_result(
        self,
        *,
        session_no: str,
        user_message: str,
        answer: str,
        selected_order_no: str | None,
        pending_action: dict[str, Any] | None,
        result: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """压缩本轮结果为最近十五轮、确定性摘要和带有效期实体。"""
        current = now or datetime.now(SHANGHAI)
        state = self.load(session_no)
        turns = list(state.get("recent_turns") or [])
        turns.extend(
            [
                {"role": "user", "content": self._safe_text(user_message), "at": current.isoformat()},
                {"role": "assistant", "content": self._safe_text(answer), "at": current.isoformat()},
            ]
        )
        overflow_count = max(0, len(turns) - RECENT_TURN_MESSAGE_LIMIT)
        state["recent_turns"] = turns[-RECENT_TURN_MESSAGE_LIMIT:]
        state["pending_action"] = pending_action
        analysis = result.get("analysis") or {}
        topic = str(analysis.get("intent") or "other")
        topics = [item for item in state["summary"].get("topics", []) if item != topic]
        state["summary"]["topics"] = (topics + [topic])[-5:]
        state["summary"]["active_topic"] = topic
        state["summary"]["last_intent"] = topic
        state["summary"]["last_user_goal"] = str(analysis.get("user_goal") or "other")
        state["summary"]["unfinished_goal"] = (
            (pending_action or {}).get("action_type") if pending_action else None
        )
        state["summary"]["unresolved_goal"] = (
            (pending_action or {}).get("action_type") if pending_action else None
        )
        self._update_last_query_summary(state["summary"], result, selected_order_no)
        if overflow_count:
            # 摘要游标只表示已有更早消息需要异步压缩，不在在线链路调用摘要模型。
            state["summary"]["summary_cursor"] = int(state["summary"].get("summary_cursor") or 0) + overflow_count
        if selected_order_no:
            self._set_order_context(state, selected_order_no, "selected_by_user", current, confirmed=True)

        # 兼容其他流程产生的确认问句：保存恢复载荷，避免下一轮“确认”丢失。
        if not state.get("pending_interaction") and "请确认" in answer and selected_order_no:
            state["pending_interaction"] = self._pending(
                "confirm_order",
                selected_order_no,
                str(analysis.get("user_goal") or "other"),
                {"goal": analysis.get("user_goal"), "original_message": user_message[:300]},
                current,
            )
        return self.save(session_no, state)

    def attach_followup(self, session_no: str, followup_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """将已创建的复核任务关联到会话，并结束当前确认交互。"""
        current = state or self.load(session_no)
        ids = list(current.get("scheduled_followups") or [])
        if followup_id not in ids:
            ids.append(followup_id)
        current["scheduled_followups"] = ids[-20:]
        current["pending_interaction"] = None
        return self.save(session_no, current)

    def await_delivery_receipt_confirmation(
        self,
        *,
        session_no: str,
        order_no: str,
        followup_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """复核后等待客户确认是否仍未收到，确认本身不能直接触发退货。"""
        current = now or datetime.now(SHANGHAI)
        state = self.load(session_no)
        state["pending_interaction"] = self._pending(
            "confirm_action",
            order_no,
            "delivery_not_received_check",
            {
                "goal": "delivery_not_received_check",
                "original_message": f"我确认仍未收到订单 {order_no}，请继续处理物流异常",
                "followup_id": followup_id,
            },
            current,
            # 定时复核属于站内通知，不应复用普通当轮确认的五分钟时限；保留三天供客户查看后确认。
            ttl_minutes=3 * 24 * 60,
        )
        return self.save(session_no, state)

    def save(self, session_no: str, state: dict[str, Any]) -> dict[str, Any]:
        """使用乐观锁保存；冲突时合并最新版本后重试一次。"""
        expected = int(state.get("version") or 0) or None
        try:
            saved = self.repository.save(session_no, state, expected_version=expected)
        except ConversationStateConflict:
            self.cache.delete(self.cache.key("session", session_no=session_no))
            latest = self.load(session_no)
            latest.update({key: value for key, value in state.items() if key not in {"version", "updated_at"}})
            saved = self.repository.save(session_no, latest, expected_version=int(latest.get("version") or 0))
        self.cache.set(self.cache.key("session", session_no=session_no), saved, 30)
        return saved

    def safe_model_context(self, state: dict[str, Any]) -> dict[str, Any]:
        """仅向模型暴露脱敏摘要和十五轮短期记忆，不暴露内部交互状态。"""
        return {
            "recent_turns": self._fit_model_turns(list(state.get("recent_turns") or [])[-RECENT_TURN_MESSAGE_LIMIT:]),
            "summary": self._safe_summary_for_model(state.get("summary") or {}),
        }

    def apply_rolling_summary(
        self,
        session_no: str,
        *,
        rolling_summary: str,
        confirmed_facts: list[str],
        summary_cursor: int,
        source_version: int,
    ) -> bool:
        """合并异步摘要；旧任务不得覆盖更新版本或更靠后的摘要游标。"""
        state = self.load(session_no)
        summary = state.get("summary") or {}
        if source_version > int(state.get("version") or 0):
            return False
        if summary_cursor < int(summary.get("summary_applied_cursor") or 0):
            return False
        summary["rolling_summary"] = self._safe_text(rolling_summary, limit=1600)
        summary["confirmed_facts"] = [self._safe_text(item, limit=160) for item in confirmed_facts[:12]]
        summary["summary_cursor"] = int(summary_cursor)
        summary["summary_applied_cursor"] = int(summary_cursor)
        summary["summary_version"] = int(summary.get("summary_version") or 0) + 1
        state["summary"] = summary
        self.save(session_no, state)
        return True

    @staticmethod
    def _resolve_operation(normalized: str) -> str | None:
        """把常见短回复归一为操作类型，避免把任务控制语义交给模型猜测。"""
        if normalized in REPEAT_WORDS:
            return "repeat"
        if normalized in CONTINUE_WORDS:
            return "continue"
        if normalized in CANCEL_WORDS:
            return "cancel"
        if normalized in CONFIRM_WORDS:
            return "confirm"
        return None

    def _restore_read_only_query(self, state: dict[str, Any], selected_order_no: str | None) -> TurnResolution | None:
        """恢复上一轮只读查询；业务写操作永远不能通过短句被重复执行。"""
        summary = state.get("summary") or {}
        last_query = summary.get("last_query") or {}
        if not last_query or not last_query.get("read_only"):
            return None
        query_type = str(last_query.get("query_type") or "")
        candidate = selected_order_no or self._usable_order_from_state(state)
        if query_type in {"order_status", "order_detail", "logistics"}:
            if not candidate:
                return TurnResolution(state=state, answer="请先选择要重新查询的订单，或提供订单号。")
            message = (
                f"查询订单 {candidate} 的最新物流状态"
                if query_type == "logistics"
                else f"查询订单 {candidate} 的当前订单状态"
            )
            return TurnResolution(state=state, resumed_message=message, order_no=candidate)
        if query_type == "ticket_status":
            ticket_no = str(last_query.get("ticket_no") or "").strip()
            if ticket_no:
                return TurnResolution(state=state, resumed_message=f"重新查询工单 {ticket_no} 的最新状态")
        return None

    @staticmethod
    def _last_operation_was_write(state: dict[str, Any]) -> bool:
        """判断上一轮是否为不可自动重放的业务写操作。"""
        summary = state.get("summary") or {}
        action = str(summary.get("last_tool_action") or "")
        return action in {"create_ticket", "urge_ticket", "refund_request", "return_goods", "exchange_goods"} or bool(
            summary.get("unfinished_goal")
        )

    def _usable_order_from_state(self, state: dict[str, Any]) -> str | None:
        """只读取未过期且已确认的会话订单，摘要中的自然语言不能提升为执行实体。"""
        order = ((state.get("entity_context") or {}).get("order") or {})
        if not order.get("order_no") or not order.get("confirmed_at"):
            return None
        expires_at = self._parse_time(order.get("expires_at"))
        if expires_at and datetime.now(SHANGHAI) > expires_at:
            return None
        return str(order.get("order_no"))

    @staticmethod
    def _update_last_query_summary(summary: dict[str, Any], result: dict[str, Any], selected_order_no: str | None) -> None:
        """从真实工具结果记录可恢复查询，禁止根据回答文本猜测工具动作。"""
        tool_results = list(result.get("tool_results") or [])
        query_type_map = {
            "order_detail": "order_status",
            "customer_orders": "order_detail",
            "order_logistics": "logistics",
            "ticket_status": "ticket_status",
            "ticket_list": "ticket_status",
        }
        query_result = next(
            (item for item in reversed(tool_results) if str(item.get("query_type") or "") in query_type_map),
            None,
        )
        if query_result:
            raw_type = str(query_result.get("query_type") or "")
            data = query_result.get("data") or {}
            summary["last_query"] = {
                "query_type": query_type_map[raw_type],
                "order_no": selected_order_no or query_result.get("order_no") or (data.get("orderNo") if isinstance(data, dict) else None),
                "ticket_no": query_result.get("ticket_no") or (data.get("ticketNo") if isinstance(data, dict) else None),
                "read_only": True,
            }
            summary["last_tool_action"] = raw_type
            summary["last_result_status"] = str(query_result.get("status") or "unknown")
            return
        ticket_result = result.get("ticket_result") or {}
        if ticket_result:
            summary["last_tool_action"] = "create_ticket"
            summary["last_result_status"] = str(ticket_result.get("status") or "unknown")

    @staticmethod
    def _safe_summary_for_model(summary: dict[str, Any]) -> dict[str, Any]:
        """白名单输出语义摘要，避免未来新增内部字段时意外进入模型。"""
        allowed = {
            "topics",
            "active_topic",
            "last_intent",
            "last_user_goal",
            "last_query",
            "last_tool_action",
            "last_result_status",
            "unfinished_goal",
            "unresolved_goal",
            "confirmed_facts",
            "rolling_summary",
            "summary_version",
        }
        return {key: summary.get(key) for key in allowed if key in summary}

    @staticmethod
    def _fit_model_turns(turns: list[dict[str, Any]], total_limit: int = 9000) -> list[dict[str, Any]]:
        """在十五轮范围内按总字符预算保留最新消息，防止异常长对话撑爆 Prompt。"""
        selected: list[dict[str, Any]] = []
        used = 0
        for item in reversed(turns):
            content = str(item.get("content") or "")[:600]
            if not content:
                continue
            if selected and used + len(content) > total_limit:
                break
            selected.append({"role": item.get("role"), "content": content, "at": item.get("at")})
            used += len(content)
        return list(reversed(selected))

    def _confirm_pending(
        self,
        session_no: str,
        state: dict[str, Any],
        pending: dict[str, Any],
        selected_order_no: str | None,
        current: datetime,
    ) -> TurnResolution:
        candidate = selected_order_no or (pending.get("candidate_entity") or {}).get("order_no")
        if not candidate:
            return TurnResolution(state=state, answer="当前没有可确认的订单，请先选择订单或提供订单号。")
        resume = dict(pending.get("resume_payload") or {})
        self._set_order_context(state, candidate, "confirmed_by_user", current, confirmed=True)
        state["pending_interaction"] = None
        self.save(session_no, state)
        if resume.get("goal") == "conditional_delivery_after_sale":
            return TurnResolution(
                state=state,
                action="schedule_delivery_recheck",
                order_no=candidate,
                scheduled_at=resume.get("scheduled_at"),
            )
        return TurnResolution(
            state=state,
            resumed_message=str(resume.get("original_message") or "请继续处理当前已确认订单"),
            order_no=candidate,
        )

    def _ask_order_confirmation(
        self,
        session_no: str,
        state: dict[str, Any],
        pending: dict[str, Any],
        selected_order_no: str,
        current: datetime,
    ) -> TurnResolution:
        state["pending_interaction"] = self._pending(
            "confirm_order",
            selected_order_no,
            str(pending.get("parent_goal") or "other"),
            dict(pending.get("resume_payload") or {}),
            current,
        )
        self._set_order_context(state, selected_order_no, "selected_by_user", current, confirmed=False)
        self.save(session_no, state)
        return TurnResolution(state=state, answer=f"请确认您说的是订单 {selected_order_no} 吗？确认后我会继续处理刚才的诉求。")

    @staticmethod
    def _pending(
        interaction_type: str,
        order_no: str | None,
        parent_goal: str,
        resume_payload: dict[str, Any],
        current: datetime,
        ttl_minutes: int = 5,
    ) -> dict[str, Any]:
        """创建待确认交互；普通对话短确认默认五分钟，异步通知可显式延长时效。"""
        return {
            "interaction_type": interaction_type,
            "candidate_entity": {"order_no": order_no} if order_no else {},
            "parent_goal": parent_goal,
            "resume_payload": resume_payload,
            "expected_reply_types": ["confirm", "cancel", "select_order"],
            "created_at": current.isoformat(),
            "expires_at": (current + timedelta(minutes=ttl_minutes)).isoformat(),
            "status": "WAITING",
        }

    @staticmethod
    def _set_order_context(state: dict[str, Any], order_no: str, source: str, current: datetime, confirmed: bool) -> None:
        state.setdefault("entity_context", {})["order"] = {
            "order_no": order_no,
            "source": source,
            "confidence": 1.0 if confirmed else 0.8,
            "confirmed_at": current.isoformat() if confirmed else None,
            "last_used_at": current.isoformat(),
            "expires_at": (current + timedelta(minutes=30)).isoformat(),
        }

    @staticmethod
    def _is_delivery_contingency(message: str) -> bool:
        text = message or ""
        delivery_condition = any(word in text for word in ("收不到", "没收到", "还没到", "未送达", "没送到"))
        future = any(word in text for word in ("明天", "后天", "到时", "届时", "如果"))
        after_sale = any(word in text for word in ("退货", "退款", "退了", "售后"))
        return delivery_condition and future and after_sale

    @staticmethod
    def _is_fuzzy_order_reference(message: str) -> bool:
        compact = re.sub(r"\s+", "", message or "")
        return compact in {"这个订单", "这个", "刚才那个", "这笔订单"}

    @staticmethod
    def _is_explicit_new_topic(message: str, parent_goal: str) -> bool:
        text = message or ""
        topics = {
            "logistics": ("查物流", "物流状态", "快递到哪"),
            "invoice": ("发票", "开票"),
            "ticket": ("工单进度", "查询工单", "催单"),
            "member": ("积分", "会员"),
        }
        return any(any(word in text for word in words) and topic not in parent_goal for topic, words in topics.items())

    def _rebuild_pending_from_history(self, state: dict[str, Any], turns: list[dict[str, Any]]) -> None:
        """兼容升级前只写了确认文本的会话，重建一次待确认状态。"""
        if state.get("pending_interaction") or not turns:
            return
        confirmation_index = -1
        candidate_order: str | None = None
        for index in range(len(turns) - 1, -1, -1):
            item = turns[index]
            content = str(item.get("content") or "")
            if item.get("role") != "assistant" or "请确认" not in content:
                continue
            match = re.search(r"\b((?:EC|SF|T)\d{6,})\b", content, flags=re.IGNORECASE)
            if match:
                confirmation_index = index
                candidate_order = match.group(1)
                break
        if confirmation_index < 0 or not candidate_order:
            return

        original_message = ""
        original_at: datetime | None = None
        for item in reversed(turns[:confirmation_index]):
            content = str(item.get("content") or "")
            if item.get("role") == "user" and self._is_delivery_contingency(content):
                original_message = content
                original_at = self._parse_time(item.get("at"))
                break
        if not original_message:
            return
        base_time = original_at or datetime.now(SHANGHAI)
        scheduled_at = self._resolve_followup_time(original_message, base_time)
        state["pending_interaction"] = self._pending(
            "confirm_order",
            candidate_order,
            "conditional_delivery_after_sale",
            {
                "goal": "conditional_delivery_after_sale",
                "original_message": original_message,
                "scheduled_at": scheduled_at.isoformat(),
                "recovered_from_history": True,
            },
            datetime.now(SHANGHAI),
        )

    @staticmethod
    def _resolve_followup_time(message: str, current: datetime) -> datetime:
        days = 2 if "后天" in message else 1
        target = (current + timedelta(days=days)).replace(hour=20, minute=0, second=0, microsecond=0)
        match = re.search(r"(上午|下午|晚上)?\s*([0-2]?\d)(?:点|:)([0-5]\d)?", message)
        if match:
            period, hour_raw, minute_raw = match.groups()
            hour = int(hour_raw)
            minute = int(minute_raw or 0)
            if period in {"下午", "晚上"} and hour < 12:
                hour += 12
            target = target.replace(hour=min(hour, 23), minute=minute)
        return target

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)
        except ValueError:
            return None

    @staticmethod
    def _safe_text(value: str, limit: int = 300) -> str:
        text = re.sub(r"Bearer\s+\S+", "[TOKEN]", str(value or ""), flags=re.IGNORECASE)
        text = re.sub(r"1[3-9]\d{9}", "[PHONE]", text)
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
        return text[:limit]
