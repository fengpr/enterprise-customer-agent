import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv


if "unittest" not in sys.modules:
    load_dotenv()


class RagConfigError(ValueError):
    """RAG 配置错误，调用方可据此决定启动失败或回退本地检索。"""


@dataclass(frozen=True)
class EmbeddingConfig:
    """Embedding 服务配置，统一 OpenAI 兼容接口和版本字段。"""

    provider: str
    model: str
    dimension: int
    distance: str
    version: str
    api_key: str | None
    base_url: str
    batch_size: int
    timeout: float
    max_retries: int


@dataclass(frozen=True)
class PgVectorConfig:
    """pgvector 检索配置，集中管理数据库、阈值和召回参数。"""

    database_url: str
    top_k_recall: int
    top_n_context: int
    min_similarity_score: float
    answerable_intent_filter: bool
    strict_startup: bool
    embedding: EmbeddingConfig


def load_embedding_config(*, require_api_key: bool = True) -> EmbeddingConfig:
    """读取 embedding 配置；入库和查询时必须具备 API Key。"""
    provider = os.getenv("EMBEDDING_PROVIDER", "openai").strip().lower()
    if provider != "openai":
        raise RagConfigError(f"Unsupported EMBEDDING_PROVIDER: {provider}")

    dimension = _int_env("EMBEDDING_DIMENSION", 1536)
    if dimension <= 0:
        raise RagConfigError("EMBEDDING_DIMENSION must be a positive integer")

    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip()
    distance = os.getenv("EMBEDDING_DISTANCE", "cosine").strip().lower()
    if distance != "cosine":
        raise RagConfigError("Only cosine distance is supported in pgvector v1")

    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    if require_api_key and not api_key:
        raise RagConfigError("EMBEDDING_API_KEY, OPENAI_API_KEY or LLM_API_KEY is required")

    base_url = (
        os.getenv("EMBEDDING_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or os.getenv("LLM_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    version = os.getenv("EMBEDDING_VERSION") or f"{provider}:{model}:{dimension}:{distance}"
    return EmbeddingConfig(
        provider=provider,
        model=model,
        dimension=dimension,
        distance=distance,
        version=version,
        api_key=api_key,
        base_url=base_url,
        batch_size=_int_env("EMBEDDING_BATCH_SIZE", 64),
        timeout=float(os.getenv("EMBEDDING_TIMEOUT", "30")),
        max_retries=_int_env("EMBEDDING_MAX_RETRIES", 3),
    )


def load_pgvector_config(*, require_api_key: bool = True) -> PgVectorConfig:
    """读取 pgvector 后端配置，生产环境缺失 DSN 时应显式失败。"""
    database_url = os.getenv("RAG_DATABASE_URL", "").strip()
    if not database_url:
        raise RagConfigError("RAG_DATABASE_URL is required when RAG_STORE_BACKEND=pgvector")
    min_score = float(os.getenv("RAG_MIN_SIMILARITY_SCORE", "0.55"))
    if not 0 <= min_score <= 1:
        raise RagConfigError("RAG_MIN_SIMILARITY_SCORE must be between 0 and 1")
    return PgVectorConfig(
        database_url=database_url,
        top_k_recall=_int_env("RAG_TOP_K_RECALL", 20),
        top_n_context=_int_env("RAG_TOP_N_CONTEXT", 5),
        min_similarity_score=min_score,
        answerable_intent_filter=_bool_env("RAG_ANSWERABLE_INTENT_FILTER", False),
        strict_startup=_bool_env("RAG_STRICT_STARTUP", False),
        embedding=load_embedding_config(require_api_key=require_api_key),
    )


def _int_env(name: str, default: int) -> int:
    """安全读取整数环境变量，避免无效配置悄悄变成默认值。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RagConfigError(f"{name} must be an integer") from exc


def _bool_env(name: str, default: bool) -> bool:
    """读取布尔环境变量，兼容常见 true/false 写法。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
