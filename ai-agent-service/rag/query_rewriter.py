"""面向知识咨询的 LLM 查询重写器。"""

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.prompts import ChatPromptTemplate

from services.llm_model_factory import ChatModelConfig, build_chat_model
from services.observability import RAG_QUERY_REWRITE_DURATION, RAG_QUERY_REWRITES
from services.resilient_client import ResilientClient, ResilientInvoker


@dataclass(frozen=True)
class QueryRewriteResult:
    """查询重写的安全结果；失败时由调用方保留规则改写。"""

    rewritten_query: str
    confidence: float


class LLMQueryRewriter:
    """仅为非实体知识咨询补全语义，不参与订单、工单或业务动作决策。"""

    def __init__(
        self,
        *,
        invoke_rewrite: Callable[[dict[str, str]], Any] | None = None,
        invoker: ResilientInvoker | None = None,
    ) -> None:
        """读取独立重写模型配置；缺失凭证时自动保持不可用状态。"""
        self.enabled = _enabled("RAG_LLM_QUERY_REWRITE_ENABLED", False)
        self.provider = os.getenv("RAG_QUERY_REWRITE_PROVIDER", "").strip().lower()
        self.model_name = os.getenv("RAG_QUERY_REWRITE_MODEL", "").strip()
        self.api_key = os.getenv("RAG_QUERY_REWRITE_API_KEY", "").strip() or None
        self.base_url = os.getenv("RAG_QUERY_REWRITE_BASE_URL", "").strip() or None
        self.invoke_rewrite = invoke_rewrite
        self.invoker = invoker or ResilientInvoker(
            ResilientClient(
                downstream="rag_query_rewrite",
                total_timeout=float(os.getenv("RAG_QUERY_REWRITE_TOTAL_TIMEOUT", "3")),
                max_retries=int(os.getenv("RAG_QUERY_REWRITE_MAX_RETRIES", "1")),
                max_concurrency=int(os.getenv("RAG_QUERY_REWRITE_MAX_CONCURRENCY", "4")),
            )
        )
        self._chain = None

    @property
    def is_available(self) -> bool:
        """只有显式启用且独立模型参数齐全时才允许调用，避免意外增加线上成本。"""
        return bool(self.enabled and (self.invoke_rewrite is not None or (self.provider and self.model_name and self.api_key)))

    def rewrite(
        self,
        *,
        query: str,
        intent: str,
        user_goal: str,
        business_scope: str | None,
        conversation_context: dict[str, Any] | None = None,
    ) -> QueryRewriteResult | None:
        """调用轻量模型把省略式问题改为独立检索问题，任何异常都返回 None。"""
        if not self.is_available:
            return None
        payload = {
            "query": _redact_text(query, limit=240),
            "intent": _safe_label(intent),
            "user_goal": _safe_label(user_goal),
            "business_scope": _safe_label(business_scope or "general"),
            **_safe_previous_turn(conversation_context),
        }
        started = time.perf_counter()
        try:
            raw = self.invoker.invoke(lambda: self._invoke(payload))
            result = _parse_result(raw)
            if not _is_valid_result(result):
                RAG_QUERY_REWRITES.labels("invalid").inc()
                return None
            RAG_QUERY_REWRITES.labels("success").inc()
            return result
        except Exception as exc:
            # 只按标准化错误分类统计，不把供应商错误体、Prompt 或客户文本写入指标。
            RAG_QUERY_REWRITES.labels(getattr(exc, "error_type", "error")).inc()
            return None
        finally:
            RAG_QUERY_REWRITE_DURATION.observe(time.perf_counter() - started)

    def _invoke(self, payload: dict[str, str]) -> Any:
        """执行模型调用；测试可注入轻量替身，避免依赖真实供应商。"""
        if self.invoke_rewrite is not None:
            return self.invoke_rewrite(payload)
        if self._chain is None:
            self._chain = self._build_chain()
        return self._chain.invoke(payload)

    def _build_chain(self):
        """构造严格 JSON 重写提示，限制模型只能改写检索语句。"""
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """
你是企业客服知识库的检索查询改写器，只输出 JSON，不输出 Markdown 或解释。
目标是把用户当前问题改成一句完整、可独立检索的知识库问题，帮助查找政策、流程、条件或时效。

严格约束：
1. 只能使用当前问题和上一轮安全摘要中已有的主题，不能补充任何订单、工单、客户身份、金额、日期、处理结果或业务事实。
2. 不得生成工具调用、操作命令、系统提示、角色指令，不能改变用户的业务域或目的。
3. 只输出一个不超过 120 个字符的中文查询；保留不确定性，不要把推测写成事实。
4. 上一轮摘要仅用于理解“这个规则、多久到账、怎么操作”等指代；当前问题优先。

返回格式：{"rewritten_query":"...","confidence":0.0}
""",
                ),
                (
                    "human",
                    "当前问题：{query}\n意图：{intent}\n用户目的：{user_goal}\n业务域：{business_scope}\n上一轮用户问题：{previous_user_question}\n上一轮客户可见回复：{previous_ai_answer}",
                ),
            ]
        )
        config = ChatModelConfig(
            provider=self.provider,
            model_name=self.model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=float(os.getenv("RAG_QUERY_REWRITE_TOTAL_TIMEOUT", "3")),
            max_retries=1,
        )
        return prompt | build_chat_model(config, temperature=0)


def _safe_previous_turn(conversation_context: dict[str, Any] | None) -> dict[str, str]:
    """仅提取上一轮客户可见消息，并先移除可识别业务标识与自称身份。"""
    memory = (conversation_context or {}).get("session_memory") or {}
    return {
        "previous_user_question": _redact_text(memory.get("last_user_question"), limit=240),
        "previous_ai_answer": _redact_text(memory.get("last_ai_answer"), limit=320),
    }


def _parse_result(raw: Any) -> QueryRewriteResult | None:
    """解析供应商消息或测试替身返回的 JSON，兼容偶发代码块包装。"""
    content = getattr(raw, "content", raw)
    if isinstance(content, list):
        content = "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    try:
        return QueryRewriteResult(
            rewritten_query=str(payload.get("rewritten_query") or "").strip(),
            confidence=float(payload.get("confidence")),
        )
    except (TypeError, ValueError):
        return None


def _is_valid_result(result: QueryRewriteResult | None) -> bool:
    """阻止低质量、敏感或疑似提示注入文本进入检索与日志。"""
    if result is None or not 0.7 <= result.confidence <= 1 or not result.rewritten_query:
        return False
    text = result.rewritten_query.strip()
    if len(text) > 120 or len(text) < 2:
        return False
    blocked = [
        r"(?i)authorization|bearer|api[_ -]?key|system prompt|ignore previous",
        r"忽略.{0,12}(指令|提示|规则)|系统提示|工具调用|<script|```",
        r"(?<![A-Za-z])(?:EC)?\d{10,18}(?!\d)|\bT\d{6,}\b",
        r"1[3-9]\d{9}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    ]
    return not any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in blocked)


def _redact_text(value: Any, *, limit: int) -> str:
    """从重写输入中移除实体标识、联系方式和会话自称，避免模型获得身份数据。"""
    text = " ".join(str(value or "").split())[:limit]
    patterns = [
        (r"(?<![A-Za-z])(?:EC)?\d{10,18}(?!\d)", "[订单已脱敏]"),
        (r"\bT\d{6,}\b", "[工单已脱敏]"),
        (r"1[3-9]\d{9}", "[手机号已脱敏]"),
        (r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已脱敏]"),
        (r"((?:我叫|我是|叫我|称呼我为)\s*)[\u4e00-\u9fa5A-Za-z·]{2,12}", r"\1[姓名已脱敏]"),
        (r"(当前登录账号(?:显示为)?\s*)[^，。；\s]{1,24}", r"\1[姓名已脱敏]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _safe_label(value: str) -> str:
    """标签字段只保留预期字符，防止外部输入进入 Prompt 的结构控制位置。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "", str(value or ""))[:40] or "general"


def _enabled(name: str, default: bool) -> bool:
    """读取显式开关；默认关闭以避免升级后无意增加模型调用成本。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
