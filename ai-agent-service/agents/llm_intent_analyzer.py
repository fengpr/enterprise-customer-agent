import os
import json
import re

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate

from agents.intent_normalizer import infer_user_goal
from schemas.intent_schema import IntentResult, LLMIntentDraft
from services.resilient_client import ResilientInvoker

load_dotenv()


class LLMIntentAnalyzer:
    """真实大模型意图识别器，负责把用户自然语言转换为业务结构化结果。"""

    def __init__(self) -> None:
        """根据环境变量初始化模型；DeepSeek 使用 LangChain 原生 ChatDeepSeek 接入。"""
        self.provider = os.getenv("LLM_PROVIDER", "openai").lower()
        self.model_name = self._resolve_model_name()
        self.analysis_temperature = float(os.getenv("LLM_ANALYSIS_TEMPERATURE", "0"))
        self.response_temperature = float(os.getenv("LLM_RESPONSE_TEMPERATURE", "0"))
        self.timeout = float(os.getenv("LLM_TIMEOUT", "25"))
        self.api_key = self._resolve_api_key()
        self.base_url = self._resolve_base_url()
        # 在线推理与 DeepEval Judge 使用不同 downstream，避免评测抢占客户回复舱壁。
        self.resilient_invoker = ResilientInvoker()
        self.chain = self._build_chain()

    @classmethod
    def is_configured(cls) -> bool:
        """判断是否已配置任一模型密钥，避免无密钥时启动阶段失败。"""
        return bool(
            os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("LLM_API_KEY")
        )

    def invoke(self, message: str) -> IntentResult:
        """调用真实 LLM 输出结构化意图结果，缺少可推导字段时由本地规则补齐。"""
        result = self.resilient_invoker.invoke(lambda: self.chain.invoke({"message": message}))
        payload = self._parse_json_payload(result)
        draft = LLMIntentDraft.model_validate(payload)
        return self._complete_intent_result(message, draft)

    def generate_customer_reply(self, payload: dict, on_delta=None) -> str:
        """基于可信业务证据生成客户侧回复，不允许模型自行编造政策、订单或物流节点。"""
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """
你是企业客户自助服务 Agent 的客户侧回复生成节点。
请根据输入中的用户问题、intent、user_goal、reply_mode、知识库 citations、订单 order、物流 logistics、工单 ticket、补充上下文 extra_context 和服务指令生成自然、具体、简洁的中文回复。

必须遵守：
1. 只能使用 citations、order、logistics、ticket 中已经提供的事实，不要编造政策、状态、金额、时间、承运商、物流节点或工单进度。
2. 如果有 order，请结合订单号、商品名、订单状态、售后状态解释用户问题；如果没有 order，不要假装已经查询订单。
3. policy_consult 只说明规则、时效、条件和下一步建议，不承诺一定退款、赔付、通过审核或具体到账日。
4. how_to 只说明操作入口和步骤，不要假装已经查询订单、物流或工单。
5. status_query 只基于工具数据说明当前状态；没有工具数据时说明暂时无法确认，不要猜测。
6. action_request、complaint、dispute 不要承诺处理结果，只说明会进入人工或受控流程；如果 ticket 存在，必须自然提到工单号和当前状态。
7. 回复面向客户，不要输出 JSON、风险原因、内部建议、工具调用字段名或“根据已发布知识库”这类机械前缀。
8. 优先回答用户真正问的点，避免扩展到无关业务，例如用户问退款到账就不要主动讲退货、维修、会员。
9. reply_mode=review_required 时，重点说明“已提交/待分派/已分派”的进度，不要说“客服正在处理”，除非 ticket.status 是 PENDING_PROCESS 或 PROCESSING。
10. reply_mode=collect_slots 时，只追问 extra_context 中要求补充的信息，不要解释整套政策，也不要说已经建单。
11. reply_mode=deduplicated_ticket 时，直接说明客户之前已经提交过相关申请，引用 ticket.ticketNo 和 ticket.status，不要说本次新建了工单。
12. 控制在 80-180 字，语气自然、专业、有人味。
13. 当使用知识库中的政策、条件、时效或流程事实时，必须紧跟对应的 citation_id，格式为【来源：kb-xxxxxxxxxxxx】；
   citation_id 必须来自输入 citations，不能虚构。没有足够证据时，明确说明暂无法确认，不得补充具体事实。
14. reply_mode=out_of_scope 时是唯一的常识回答例外：可以使用模型掌握的稳定、低风险通用知识，先用一到两句直接回答问题，
    再自然生成一句服务边界提醒，不要套用固定模板；边界提醒应覆盖订单、物流、售后、工单、发票中的至少两个业务范围。
    不要自我介绍成“企业客服助手”，也不要使用“我主要负责/主要处理”这类生硬身份声明。
    不得查询或引用订单、物流、工单、知识库和历史业务上下文，
    不得自动转人工或声称已创建工单；医疗、法律、金融投资和危险操作不得在此模式下给出具体建议。
""",
                ),
                (
                    "human",
                    """
用户问题：{message}
业务域 intent：{intent}
用户目的 user_goal：{user_goal}
问题摘要：{summary}
回复模式 reply_mode：{reply_mode}
服务指令：{service_instruction}
知识库证据：{citations}
订单证据：{order}
物流证据：{logistics}
工单证据：{ticket}
补充上下文：{extra_context}
""",
                ),
            ]
        )
        reply_chain = prompt | self._build_llm(self.response_temperature)
        # SSE 场景直接消费模型原生 stream；非流式调用仍保持既有 invoke 语义。
        if on_delta is None:
            result = self.resilient_invoker.invoke(lambda: reply_chain.invoke(payload))
            content = getattr(result, "content", result)
            if isinstance(content, list):
                content = "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
            text = str(content).strip()
        else:
            pieces: list[str] = []
            for chunk in self.resilient_invoker.stream(lambda: reply_chain.stream(payload)):
                content = getattr(chunk, "content", chunk)
                if isinstance(content, list):
                    content = "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
                delta = str(content)
                if delta:
                    pieces.append(delta)
                    # 仅发送模型文本片段，不发送 LangChain 消息对象或供应商元数据。
                    on_delta(delta)
            text = "".join(pieces).strip()
        # 回复节点只允许返回纯客户话术，剥离模型偶发产生的代码块包裹。
        return re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()

    def _resolve_model_name(self) -> str:
        """根据服务商选择默认模型，避免 DeepSeek 配置时仍误用 OpenAI 默认模型名。"""
        if os.getenv("LLM_MODEL"):
            return os.getenv("LLM_MODEL", "")
        if self.provider == "deepseek":
            return "deepseek-v4-flash"
        return "gpt-4o-mini"

    def _resolve_api_key(self) -> str | None:
        """按 DeepSeek 优先级读取密钥，便于本地只配置 DEEPSEEK_API_KEY。"""
        if self.provider == "deepseek":
            return os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY")
        return os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")

    def _resolve_base_url(self) -> str | None:
        """读取 OpenAI 兼容服务的 base_url；ChatDeepSeek 原生接入不需要该参数。"""
        if self.provider == "deepseek":
            return None
        if os.getenv("LLM_BASE_URL"):
            return os.getenv("LLM_BASE_URL")
        if os.getenv("OPENAI_BASE_URL"):
            return os.getenv("OPENAI_BASE_URL")
        return None

    def _build_chain(self):
        """构建 LangChain 结构化输出链，模型返回会被 Pydantic Schema 校验。"""
        llm = self._build_llm(self.analysis_temperature)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """
你是企业客服工单 Agent 的结构化意图识别节点。
必须只根据用户输入输出符合 Schema 的 JSON 结构化结果，不要生成客服回复。
必须输出一个 JSON object，不要输出 Markdown、解释文字或代码块。

必须包含字段：
intent, user_goal, emotion, order_related, order_no, product_name, need_order_query, need_ticket,
need_human, priority, confidence, summary, risk_reasons, action_type, action_slots, missing_slots, next_action。

枚举约束：
- intent: consult, logistics, refund, exchange, repair, complaint, invoice, member, other
- user_goal: policy_consult, how_to, status_query, action_request, human_request, out_of_scope, complaint, dispute, info_query, other
- emotion: normal, anxious, dissatisfied, strong_complaint
- priority: low, medium, high, urgent
- action_type: return_goods, refund_request, exchange_goods, repair_request, invoice_issue, cancel_order, complaint_submit, other
- next_action: collect_slots, validate_order, call_business_tool, create_ticket, ask_clarification, transfer_human, cancel_pending, unsupported

业务规则：
1. intent 表示业务域，不表示是否高风险；user_goal 表示用户真实目的。
2. 规则、条件、时效、政策类问题用 user_goal=policy_consult，例如“退款多久到账”“换货规则”“会员权益”。
3. 操作步骤类问题用 user_goal=how_to，例如“怎么查询物流状态”“在哪里看快递”“怎么申请退货”“发票怎么开”。
4. 查真实进度、查真实状态类问题用 user_goal=status_query，例如“帮我查物流”“物流到哪了”“维修进度”“发票开好了吗”“退款进度”。
5. 要求系统执行业务动作时用 user_goal=action_request，例如“我要退货”“我要退款”“帮我换货”“申请维修”“开一张发票”。
6. 用户明确说“转人工、人工客服、找真人、我要人工处理”时用 user_goal=human_request。
7. 非客服业务问题用 user_goal=out_of_scope，例如“天空为什么是蓝色的”“讲个笑话”“推荐股票”。
8. 投诉、举报、维权、赔付、强烈不满用 user_goal=complaint；商家拒绝、售后争议用 user_goal=dispute。
9. 出现“送达、到达、没收到、物流、快递、配送、发货、签收、订单到哪、路线、转运、转运站、经过哪里、全流程”等表达时，intent=logistics。
10. 出现“工单进度、催工单、催一下、加急、T 开头工单号”等表达时，这是工单状态查询或催办，user_goal=status_query，need_ticket=false，不要创建新工单。
11. 物流、退款、换货、维修或出现订单号时，order_related=true；只有 T 开头工单号时不要当成订单号。
12. user_goal=policy_consult、how_to、status_query、info_query、out_of_scope 且无投诉争议时，通常 need_human=false、need_ticket=false。
13. user_goal=human_request、action_request、complaint、dispute 或涉及赔付/法律风险时，need_human=true。
14. 置信度低于 0.7 时 need_human=true，并在 risk_reasons 中加入 low_confidence，但 out_of_scope 不因低置信度转人工。
15. 不确定时 intent=other，user_goal=other，confidence 不要高于 0.6。
16. summary 用中文概括用户问题，不超过 120 字。
17. action_request 必须给出 action_type 和 action_slots；政策咨询和操作步骤咨询不要进入动作闭环。
18. “我要退货/帮我换货/申请维修/开一张发票”是 action_request；“怎么退货/退货规则是什么/发票怎么开”不是 action_request。
19. 槽位命名使用通用字段：order_no, product_name, description, after_sale_reason, return_method, pickup_time_window, fault_description, invoice_title, invoice_type, tax_no, evidence_hint。
20. 退货时必须从整条消息一次提取全部已提供信息：原因写入 after_sale_reason；“上门取件”写 return_method=pickup；“自行寄回/自己寄回”写 return_method=self_ship；客户提供的取件时间只截取时间短语写入 pickup_time_window。
21. “商品有问题、用不上了、不合适、不喜欢”等可以作为 after_sale_reason，但单独出现时不代表客户已经授权创建退货工单；是否执行由后端状态机判断。
22. 信息不全时 next_action=collect_slots；信息齐全时可以建议 create_ticket，但最终是否查单或建单只能由后端确定性规则决定。

JSON 示例：
{{
  "intent": "logistics",
  "user_goal": "status_query",
  "emotion": "normal",
  "order_related": true,
  "order_no": ["EC202606220001"],
  "product_name": null,
  "need_order_query": true,
  "need_ticket": false,
  "need_human": false,
  "priority": "medium",
  "confidence": 0.9,
  "summary": "用户咨询订单物流未送达原因",
  "risk_reasons": [],
  "action_type": null,
  "action_slots": {{}},
  "missing_slots": [],
  "next_action": null
}}
""",
                ),
                ("human", "{message}"),
            ]
        )
        # 不使用 with_structured_output，避免不同模型供应商的 JSON schema 适配差异导致识别整体降级。
        # 这里让大模型直接输出 JSON，再由本地 Pydantic 做最终校验和补齐。
        return prompt | llm

    @staticmethod
    def _parse_json_payload(result) -> dict:
        """从模型消息中解析 JSON，兼容模型偶尔包裹 Markdown 代码块的情况。"""
        content = getattr(result, "content", result)
        if isinstance(content, list):
            content = "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
        text = str(content).strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                raise
            return json.loads(match.group(0))

    def _build_llm(self, temperature: float):
        """根据 Provider 创建模型实例，DeepSeek 分支必须使用 ChatDeepSeek。"""
        if self.provider == "deepseek":
            try:
                from langchain_deepseek import ChatDeepSeek
            except ImportError as exc:
                raise RuntimeError("缺少 langchain-deepseek 依赖，无法启用 ChatDeepSeek。") from exc

            # ChatDeepSeek 直接调用 DeepSeek 官方接口，不再经过 ChatOpenAI 兼容层。
            return ChatDeepSeek(
                model=self.model_name,
                temperature=temperature,
                api_key=self.api_key,
                timeout=self.timeout,
                max_retries=1,
            )

        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise RuntimeError("缺少 langchain-openai 依赖，无法启用 OpenAI 意图识别。") from exc

        model_kwargs = {
            "model": self.model_name,
            "temperature": temperature,
            "api_key": self.api_key,
            "timeout": self.timeout,
            "max_retries": 1,
        }
        if self.base_url:
            # 其它 OpenAI 兼容服务通过 base_url 接入，DeepSeek 原生分支不走这里。
            model_kwargs["base_url"] = self.base_url
        return ChatOpenAI(**model_kwargs)

    def _complete_intent_result(self, message: str, draft: LLMIntentDraft) -> IntentResult:
        """补齐 LLM 缺失的可推导字段，只有不可修复字段才交给 Pydantic 抛错。"""
        # LLM 偶尔会把无订单号输出为 null，这不应导致整轮意图识别降级。
        order_no = list(draft.order_no or [])
        import re

        for item in re.findall(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE):
            if item not in order_no:
                order_no.append(item)

        order_related = draft.order_related or bool(order_no) or draft.intent in {
            "logistics",
            "refund",
            "exchange",
            "repair",
        }
        user_goal = self._infer_user_goal(message, draft.user_goal)
        need_order_query = draft.need_order_query
        if need_order_query is None:
            need_order_query = order_related
        if user_goal in {"policy_consult", "how_to"}:
            # 规则咨询和操作步骤咨询优先回答方法，不要求客户先补充订单号。
            need_order_query = False

        need_human = draft.need_human
        priority = draft.priority
        risk_reasons = list(draft.risk_reasons)

        if user_goal in {"human_request", "action_request", "complaint", "dispute"} or draft.intent == "complaint" or draft.emotion == "strong_complaint":
            need_human = True
            priority = "high" if priority in {"low", "medium"} else priority
            reason = "human_request" if user_goal == "human_request" else "complaint" if user_goal == "complaint" or draft.intent == "complaint" else "action_or_dispute_requires_human"
            if reason not in risk_reasons:
                risk_reasons.append(reason)
        elif user_goal in {"policy_consult", "how_to", "status_query", "info_query", "out_of_scope"}:
            # 规则咨询、操作步骤咨询和只读查询不因命中高风险业务域而直接转人工。
            need_human = False
            if "refund_commitment" in risk_reasons:
                risk_reasons.remove("refund_commitment")

        if draft.confidence < 0.7 and user_goal not in {"out_of_scope"}:
            need_human = True
            if "low_confidence" not in risk_reasons:
                risk_reasons.append("low_confidence")

        need_ticket = draft.need_ticket
        if need_ticket is None:
            need_ticket = need_human or user_goal in {"human_request", "action_request", "complaint", "dispute"} or draft.intent == "complaint"
        if user_goal in {"policy_consult", "how_to", "status_query", "info_query", "out_of_scope"} and draft.emotion != "strong_complaint":
            need_ticket = False

        summary = draft.summary or message[:120]

        if user_goal in {"policy_consult", "how_to"}:
            order_related = False

        return IntentResult(
            intent=draft.intent,
            user_goal=user_goal,
            emotion=draft.emotion,
            order_related=order_related,
            order_no=order_no,
            product_name=draft.product_name,
            need_order_query=need_order_query,
            need_ticket=need_ticket,
            need_human=need_human,
            priority=priority,
            confidence=draft.confidence,
            summary=summary[:120],
            risk_reasons=risk_reasons,
            action_type=draft.action_type,
            action_slots=draft.action_slots or {},
            missing_slots=draft.missing_slots or [],
            next_action=draft.next_action,
        )

    @staticmethod
    def _infer_user_goal(message: str, draft_goal: str) -> str:
        """根据关键词修正模型的用户目的，避免业务域被误当成高风险动作。"""
        return infer_user_goal(message, draft_goal)
