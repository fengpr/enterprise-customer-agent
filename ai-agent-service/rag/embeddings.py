import time
from collections.abc import Sequence

import httpx

from rag.pg_config import EmbeddingConfig, RagConfigError, load_embedding_config


class OpenAICompatibleEmbeddingClient:
    """OpenAI 兼容 embedding 客户端，支持批量请求和失败重试。"""

    def __init__(self, config: EmbeddingConfig | None = None) -> None:
        """初始化 embedding 客户端，配置缺失时在创建阶段明确失败。"""
        self.config = config or load_embedding_config()

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化文本，保持返回顺序与输入顺序一致。"""
        if not texts:
            return []
        embeddings: list[list[float]] = []
        for start in range(0, len(texts), self.config.batch_size):
            batch = list(texts[start : start + self.config.batch_size])
            embeddings.extend(self._embed_batch(batch))
        return embeddings

    def embed_query(self, text: str) -> list[float]:
        """向量化单条查询文本，供 pgvector 检索使用。"""
        return self.embed_texts([text])[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """请求 OpenAI 兼容 embeddings 接口，并对临时错误做指数退避。"""
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = httpx.post(
                    f"{self.config.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json={"model": self.config.model, "input": texts},
                    timeout=self.config.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                data = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
                vectors = [item["embedding"] for item in data]
                self._validate_vectors(vectors, expected=len(texts))
                return vectors
            except Exception as exc:  # noqa: BLE001 - 需要把 HTTP/JSON/维度错误统一重试或抛出
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                time.sleep(min(2**attempt, 8))
        raise RagConfigError(f"Embedding request failed: {last_error}") from last_error

    def _validate_vectors(self, vectors: list[list[float]], *, expected: int) -> None:
        """校验 embedding 返回数量和维度，避免脏向量进入数据库。"""
        if len(vectors) != expected:
            raise RagConfigError(f"Embedding result count mismatch: expected {expected}, got {len(vectors)}")
        for vector in vectors:
            if len(vector) != self.config.dimension:
                raise RagConfigError(
                    f"Embedding dimension mismatch: expected {self.config.dimension}, got {len(vector)}"
                )


def vector_literal(vector: Sequence[float]) -> str:
    """把 Python 向量转换为 pgvector 可识别的文本字面量。"""
    return "[" + ",".join(f"{float(item):.8f}" for item in vector) + "]"
