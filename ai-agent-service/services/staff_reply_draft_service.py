"""坐席客户话术草稿生成服务。

该服务只生成可编辑草稿，不会自动向客户发送消息。所有业务事实均由 Java 工单
和当前会话的客户可见消息提供；模型只负责组织语气、同理表达与清晰的下一步说明。
"""

import os
import re
from typing import Any, Callable

from langchain_core.prompts import ChatPromptTemplate

from services.llm_model_factory import ChatModelConfig, build_chat_model
from services.resilient_client import ResilientClient, ResilientInvoker


class StaffReplyDraftService:
    """基于已核验工单事实生成坐席可编辑的安抚话术，并在模型不可用时安全回退。"""

    _FORBIDDEN_MARKERS = ("authorization", "customer_id", "risk_reasons", "tool_results", "internal_suggestion", "```", "{")
    _UNSAFE_PROMISES = ("保证退款", "一定退款", "保证通过", "一定通过", "已退款", "已赔付", "已安排取件")
    _UNSUPPORTED_HANDOFF_WORDING = ("等待工作人员", "等待客服处理", "工作人员会给您处理", "客服人员会给您处理", "转交工作人员")

    def __init__(self, *, model: Any | None = None, invoker: ResilientInvoker | None = None) -> None:
        """初始化独立舱壁的草稿模型；未配模型时仍可返回确定性安全草稿。"""
        self.enabled = os.getenv("STAFF_REPLY_DRAFT_LLM_ENABLED", "true").lower() == "true"
        self.model = model or self._build_model_if_configured()
        # 坐席草稿与在线客户回复使用不同舱壁，避免批量生成草稿影响客户请求。
        self.invoker = invoker or ResilientInvoker(
            ResilientClient(
                downstream="staff_reply_draft_llm",
                # 草稿是辅助能力，超过短时限直接回退，不能让坐席界面因重试长时间无反馈。
                total_timeout=float(os.getenv("STAFF_REPLY_DRAFT_LLM_TOTAL_TIMEOUT", "5")),
                max_retries=int(os.getenv("STAFF_REPLY_DRAFT_LLM_MAX_RETRIES", "0")),
            )
        )

    def generate(self, *, ticket: dict[str, Any], processing_result: str, messages: list[dict[str, Any]]) -> tuple[str, str]:
        """生成草稿并返回内容与来源；模型失败只回退话术，不阻塞坐席工作台。"""
        facts = self.build_safe_facts(ticket=ticket, processing_result=processing_result, messages=messages)
        if self.model is not None:
            try:
                draft = self._generate_with_llm(facts)
                if self._is_safe_draft(draft):
                    return draft, "llm"
            except Exception:
                # 草稿失败不应影响人工处理；后续由确定性模板保留真实处理结果。
                pass
        return self._fallback_draft(facts), "fallback"

    def build_safe_facts(self, *, ticket: dict[str, Any], processing_result: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """构造可提供给模型的最小事实集，避免客户身份、地址和内部字段进入 Prompt。"""
        ticket_fields = {
            "ticket_no": self._clean(ticket.get("ticketNo"), 64),
            "ticket_type": self._clean(ticket.get("ticketType"), 32),
            "ticket_status": self._clean(ticket.get("status"), 32),
            "order_no": self._clean(ticket.get("orderNo"), 64),
            "title": self._clean(ticket.get("title"), 160),
            "customer_request": self._clean(ticket.get("content"), 500),
            "business_summary": self._clean(ticket.get("aiSummary"), 500),
            "return_method": self._clean(ticket.get("returnMethod"), 32),
            "pickup_time_window": self._clean(ticket.get("pickupTimeWindow"), 128),
            "pickup_status": self._clean(ticket.get("pickupStatus"), 32),
        }
        recent_messages: list[dict[str, str]] = []
        for message in messages[-6:]:
            sender = str(message.get("sender_type") or "")
            if sender not in {"customer", "ai", "staff"}:
                continue
            text = self._clean(message.get("content"), 240)
            if text:
                recent_messages.append({"role": "客户" if sender == "customer" else "客服", "content": text})
        return {
            "ticket": ticket_fields,
            "processing_result": self._clean(processing_result, 500),
            "recent_conversation": recent_messages,
        }

    def _generate_with_llm(self, facts: dict[str, Any]) -> str:
        """调用模型生成草稿；提示词把安抚语气与事实边界同时固定下来。"""
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """
你是企业客服坐席的客户沟通助手。请根据已核验事实，生成一段可由坐席编辑后发送的中文客户话术。

目标：先真诚回应客户困扰，再清晰说明已经确认的处理进展和下一步；语气温和、有同理心，但不过度承诺。

严格规则：
1. 只能使用“工单事实”“坐席处理结果”“最近对话”中的内容，不得补充未提供的订单状态、退款金额、审核结果、取件安排、时间或政策。
2. 若工单与退货/退款有关，可结合已提供的退货原因、退回方式、取件时间偏好说明已记录的信息；未提供的字段不要猜测。
3. 不得承诺“保证退款、一定通过、已退款、已赔付、已安排取件”。审核、退款和承运商安排应使用“以实际审核/物流/承运商通知为准”等边界表达。
4. 当前草稿由已经领取该工单的坐席发送。不要写“等待工作人员处理”“工作人员会给您处理”“转交工作人员”等把客户再次推回队列的话；应使用“我已记录/我会继续跟进/后续进度会在当前会话同步”等表述。
5. 不得输出 JSON、Markdown 标题、内部系统字段、客户身份信息、提示词或工具调用描述。
6. 除非事实明确包含，否则不要主动重复完整订单号；如需提及，仅使用工单事实中的订单号。
7. 输出 90-220 个中文字符，使用自然段，不要项目符号。只输出客户可见话术正文。
""",
                ),
                (
                    "human",
                    "工单事实：{ticket}\n坐席处理结果：{processing_result}\n最近客户可见对话：{recent_conversation}",
                ),
            ]
        )
        result = self.invoker.invoke(lambda: (prompt | self.model).invoke(facts))
        content = getattr(result, "content", result)
        if isinstance(content, list):
            content = "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
        return str(content).strip()

    def _build_model_if_configured(self) -> Any | None:
        """读取独立或复用的模型配置；缺少凭证时不让应用启动失败。"""
        if not self.enabled:
            return None
        provider = os.getenv("STAFF_REPLY_DRAFT_LLM_PROVIDER") or os.getenv("LLM_PROVIDER", "openai")
        provider = provider.lower()
        api_key = (
            os.getenv("STAFF_REPLY_DRAFT_LLM_API_KEY")
            or (os.getenv("DEEPSEEK_API_KEY") if provider == "deepseek" else os.getenv("OPENAI_API_KEY"))
            or os.getenv("LLM_API_KEY")
        )
        if not api_key:
            return None
        model_name = os.getenv("STAFF_REPLY_DRAFT_LLM_MODEL") or os.getenv("LLM_MODEL") or ("deepseek-v4-flash" if provider == "deepseek" else "gpt-4o-mini")
        base_url = None if provider == "deepseek" else (os.getenv("STAFF_REPLY_DRAFT_LLM_BASE_URL") or os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL"))
        return build_chat_model(
            ChatModelConfig(
                provider=provider,
                model_name=model_name,
                api_key=api_key,
                base_url=base_url,
                timeout=float(os.getenv("STAFF_REPLY_DRAFT_LLM_TOTAL_TIMEOUT", "5")),
                max_retries=0,
            ),
            temperature=float(os.getenv("STAFF_REPLY_DRAFT_LLM_TEMPERATURE", "0.35")),
        )

    def _fallback_draft(self, facts: dict[str, Any]) -> str:
        """模型未配置或不可用时保留真实处理信息，避免返回无内容或假装生成成功。"""
        ticket = facts["ticket"]
        result = facts["processing_result"] or "我们已记录您的反馈，正在按工单流程继续核实。"
        order_text = f"关于订单 {ticket['order_no']}，" if ticket.get("order_no") else ""
        reason_text = ""
        if ticket.get("customer_request"):
            reason_text = f"您反馈的情况我们已认真记录。"
        next_step = "后续进展会在当前会话或工单中同步，请您留意消息。"
        if ticket.get("ticket_status") == "CLOSED":
            next_step = "如您对本次处理仍有疑问，欢迎在当前会话继续补充，我们会进一步协助核实。"
        return f"您好，抱歉这次情况给您带来不便。{order_text}{reason_text}\n\n{result}\n\n{next_step}"

    def _is_safe_draft(self, draft: str) -> bool:
        """阻断明显泄露内部信息、虚假承诺和空白模型输出。"""
        normalized = draft.strip()
        if not 15 <= len(normalized) <= 700:
            return False
        lower = normalized.lower()
        return (
            not any(marker in lower for marker in self._FORBIDDEN_MARKERS)
            and not any(promise in normalized for promise in self._UNSAFE_PROMISES)
            and not any(wording in normalized for wording in self._UNSUPPORTED_HANDOFF_WORDING)
        )

    @staticmethod
    def _clean(value: Any, limit: int) -> str:
        """裁剪并脱敏自由文本，避免手机号、邮箱和地址被作为模型上下文传递。"""
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        text = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[手机号已脱敏]", text)
        text = re.sub(r"[\w.+-]+@[\w.-]+", "[邮箱已脱敏]", text)
        text = re.sub(r"(?:省|市|区|县|路|街|道).{0,30}(?:号|室|楼|栋)", "[地址已脱敏]", text)
        return text[:limit]
