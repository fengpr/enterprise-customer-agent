import re
from typing import Literal


UserGoalValue = Literal["policy_consult", "how_to", "status_query", "action_request", "human_request", "out_of_scope", "complaint", "dispute", "info_query", "other"]
IntentValue = Literal["consult", "logistics", "refund", "exchange", "repair", "complaint", "invoice", "member", "other"]


def infer_user_goal(message: str, current_goal: str = "other") -> UserGoalValue:
    """按用户目标优先级规范化 user_goal，避免后续工具路由被错误意图带偏。"""
    if is_identity_message(message):
        return "info_query"
    if is_human_request_message(message):
        return "human_request"
    if is_order_statistics_message(message):
        # 购买汇总属于真实数据查询，优先级高于“怎么查看”等泛操作指引判断。
        return "info_query"
    if contains_any(message, ["投诉", "举报", "维权", "赔付", "赔偿", "差评"]):
        return "complaint"
    if contains_any(message, ["不给", "拒绝", "不处理", "太慢", "一直没", "扯皮"]):
        return "dispute"
    if is_action_request_message(message):
        return "action_request"
    if is_how_to_message(message):
        return "how_to"
    if is_status_query_message(message):
        return "status_query"
    if is_policy_consult_message(message):
        return "policy_consult"
    if is_out_of_scope_message(message):
        return "out_of_scope"
    if current_goal != "other":
        return current_goal  # type: ignore[return-value]
    return "out_of_scope"


def infer_intent(message: str, current_intent: str = "other") -> IntentValue:
    """按业务域关键词规范化 intent，intent 只表达业务范围，不表达动作风险。"""
    if current_intent not in {"other", "consult"}:
        return current_intent  # type: ignore[return-value]
    if is_logistics_message(message) or is_order_query_message(message):
        return "logistics"
    if contains_any(message, ["退货", "退款", "退钱", "取消订单", "七天无理由"]):
        return "refund"
    if contains_any(message, ["换货", "更换", "型号不对", "质量不好", "质量问题", "不给更换", "不给换"]):
        return "exchange"
    if contains_any(message, ["维修", "报修", "修一下", "坏了", "故障", "无法使用", "连接不上", "报错"]):
        return "repair"
    if contains_any(message, ["投诉", "举报", "差评", "欺骗", "维权"]):
        return "complaint"
    if contains_any(message, ["发票", "抬头", "税号", "开票"]):
        return "invoice"
    if contains_any(message, ["会员", "权益", "续费", "到期"]):
        return "member"
    if current_intent == "consult":
        return "consult"
    return "other"


def is_how_to_message(message: str) -> bool:
    """识别操作步骤咨询，只说明怎么做，不代表客户要求系统立即代办或查询。"""
    if contains_any(message, ["怎么还没", "为什么还没", "怎么不到账", "怎么没到"]):
        return False
    return contains_any(
        message,
        [
            "怎么查询",
            "如何查询",
            "怎样查询",
            "怎么查看",
            "如何查看",
            "怎样查看",
            "在哪里看",
            "在哪看",
            "哪里看",
            "从哪里看",
            "怎么操作",
            "如何操作",
            "怎么申请",
            "如何申请",
            "怎么开",
            "如何开",
            "怎么退",
            "如何退",
        ],
    )


def is_human_request_message(message: str) -> bool:
    """识别客户明确要求真人客服介入的表达。"""
    return contains_any(message, ["转人工", "人工客服", "找人工", "找真人", "真人客服", "人工处理", "转接客服", "转接人工", "我要人工", "需要人工"])


def is_out_of_scope_message(message: str) -> bool:
    """识别非客服业务范围问题，避免被知识库缺失误判为转人工。"""
    if _has_business_keyword(message) or is_identity_message(message) or is_human_request_message(message):
        return False
    return True


def is_high_risk_out_of_scope_message(message: str) -> bool:
    """识别医疗、法律、金融和危险操作等高风险越界问题。"""
    return contains_any(
        message,
        [
            "心脏疼",
            "胸痛",
            "吃什么药",
            "用药",
            "诊断",
            "病",
            "律师",
            "法律责任",
            "逃避法律",
            "起诉",
            "股票",
            "基金",
            "投资",
            "理财",
            "贷款",
            "炸药",
            "武器",
            "破解",
            "黑客",
            "自杀",
            "伤害自己",
        ],
    )


def is_status_query_message(message: str) -> bool:
    """识别真实状态查询，后续可使用选中订单或上下文调用只读工具。"""
    if is_how_to_message(message):
        return False
    return contains_any(
        message,
        ["帮我查", "查一下", "查询一下", "查查", "催办", "催一下", "帮我催", "进度", "状态", "到哪", "开好了吗", "到了吗", "什么时候到", "还没到账", "物流到哪"],
    )


def is_policy_consult_message(message: str) -> bool:
    """识别规则、政策、条件和时效咨询。"""
    return contains_any(message, ["规则", "政策", "流程", "多久", "多久到账", "多久退", "条件", "权益", "要多久", "能不能", "可以吗"])


def is_action_request_message(message: str) -> bool:
    """识别真实业务动作请求，排除步骤咨询和政策咨询。"""
    if is_how_to_message(message) or is_policy_consult_message(message) or is_status_query_message(message):
        return False
    return contains_any(
        message,
        ["我要", "帮我", "申请", "开一张", "开发票", "开票", "开个人发票", "开企业发票", "取消订单", "处理一下", "给我退", "给我换", "想退", "想换", "要退", "要换"],
    )


def is_identity_message(message: str) -> bool:
    """识别客户询问智能客服身份或能力的闲聊式问题。"""
    text = re.sub(r"[\s？?。！!，,；;：:]+", "", message.strip().lower())
    if not text:
        return False
    exact_phrases = {
        "你是谁",
        "你是誰",
        "你是什么",
        "你是机器人吗",
        "你是ai吗",
        "你是客服吗",
        "你能做什么",
        "你会做什么",
        "你可以做什么",
        "你有什么功能",
        "你有什么能力",
        "你能干什么",
        "你会干什么",
        "你可以干什么",
        "你能帮什么",
        "你能帮我什么",
        "你可以帮我什么",
        "介绍一下你自己",
        "whoareyou",
    }
    return text in exact_phrases or contains_any(text, ["智能客服是谁", "客服助手是谁", "你能帮我做什么", "你可以帮我做什么", "能力介绍", "功能介绍"])


def is_logistics_message(message: str) -> bool:
    """识别物流配送类业务域表达。"""
    return contains_any(
        message,
        ["物流", "快递", "配送", "发货", "签收", "送达", "到达", "到哪", "没收到", "没有收到", "还没到", "还没有到", "什么时候到", "订单到哪", "转运", "路线", "全流程", "运单"],
    )


def is_delivery_not_received_message(message: str) -> bool:
    """识别实体包裹未收到反馈，排除退款到账和电子发票等非物流语境。"""
    if contains_any(message, ["退款", "款项", "到账", "银行卡", "余额"]):
        return False
    if "发票" in message and not contains_any(message, ["纸质", "邮寄", "快递"]):
        return False
    return contains_any(
        message,
        ["没收到", "没有收到", "未收到", "还没收到", "还没有收到", "包裹没到", "快递没到", "货没到"],
    )


def is_order_query_message(message: str) -> bool:
    """识别泛订单查询表达。"""
    return is_order_statistics_message(message) or is_order_detail_query_message(message) or contains_any(
        message,
        ["查询我的订单", "查我的订单", "我的订单", "查询订单", "查订单", "订单列表", "订单记录", "买过什么", "买了什么", "买了哪些", "购买记录"],
    )


def is_order_detail_query_message(message: str) -> bool:
    """识别针对某笔订单或其中商品的事实查询，不把商品故障、投诉等动作诉求误归为详情查询。"""
    text = re.sub(r"[\s？?。！!，,；;：:]+", "", message.strip().lower())
    if not text or contains_any(text, ["坏了", "故障", "质量问题", "投诉", "退货", "退款", "换货", "维修"]):
        return False
    entity_reference = contains_any(
        text,
        ["该订单", "这个订单", "这笔订单", "当前订单", "这单", "订单的商品", "订单商品", "这件商品", "当前商品"],
    )
    detail_goal = contains_any(
        text,
        ["介绍", "详情", "详细信息", "商品信息", "产品信息", "是什么商品", "什么商品", "商品名称", "产品名称", "多少钱", "价格", "金额", "数量", "几件", "分类", "保修", "质保", "支持退货"],
    )
    return entity_reference and detail_goal


def is_order_statistics_message(message: str) -> bool:
    """识别需要查询真实订单并汇总件数、金额或商品清单的表达。"""
    purchase_scope = contains_any(message, ["买了", "买过", "购买", "订单", "消费", "花费", "实付"])
    aggregate_goal = contains_any(
        message,
        ["统计", "算一下", "哪些东西", "哪些商品", "买了什么", "买过什么", "买了哪些", "一共几件", "总共几件", "多少件", "一共多少", "总共多少", "总计", "合计", "花了多少", "花费多少", "消费多少"],
    )
    return purchase_scope and aggregate_goal


def _has_business_keyword(message: str) -> bool:
    """识别客服业务关键词，用于区分业务问题和普通越界问题。"""
    return any(
        checker(message)
        for checker in [
            is_logistics_message,
            is_order_query_message,
            is_order_statistics_message,
            is_policy_consult_message,
            is_status_query_message,
            is_how_to_message,
        ]
    ) or contains_any(message, ["订单", "工单", "售后", "退货", "退款", "换货", "维修", "发票", "会员", "客服", "商品", "质量", "商家", "投诉", "举报", "维权", "赔付", "赔偿", "差评"])


def contains_any(text: str, words: list[str]) -> bool:
    """判断文本是否包含任一关键词。"""
    return any(word in text for word in words)
