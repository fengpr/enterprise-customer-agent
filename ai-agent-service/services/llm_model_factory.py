"""复用的 LangChain 聊天模型构建能力。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatModelConfig:
    """描述一个独立模型调用链所需的非敏感配置。"""

    provider: str
    model_name: str
    api_key: str | None
    base_url: str | None
    timeout: float
    max_retries: int = 1


def build_chat_model(config: ChatModelConfig, *, temperature: float):
    """按供应商构建 LangChain 模型，供意图、回复和检索重写复用。"""
    if config.provider.strip().lower() == "deepseek":
        try:
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:
            raise RuntimeError("缺少 langchain-deepseek 依赖，无法启用 ChatDeepSeek。") from exc
        # DeepSeek 原生客户端不使用 OpenAI 兼容 base_url 参数。
        return ChatDeepSeek(
            model=config.model_name,
            temperature=temperature,
            api_key=config.api_key,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("缺少 langchain-openai 依赖，无法启用 OpenAI 兼容模型。") from exc

    kwargs = {
        "model": config.model_name,
        "temperature": temperature,
        "api_key": config.api_key,
        "timeout": config.timeout,
        "max_retries": config.max_retries,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    return ChatOpenAI(**kwargs)
