"""Embedding：把文本变成向量。

两种实现，按 .env 自动选择（**调用方无感知**）：

1. OpenAICompatibleEmbeddings —— 配置了 EMBEDDING_API_KEY/BASE_URL/MODEL 时使用。
   DeepSeek 官方**不提供** embedding 接口，所以要单独接一个 OpenAI 兼容的
   embedding 服务（通义/智谱/OpenAI/jina 等都可以）。

2. LocalHashEmbeddings —— 没配 key 时的降级实现：
   字符 n-gram 哈希到固定维度 + L2 归一化。
   它是"词法相似度"而不是"语义相似度"：问"七天无理由是什么意思"能命中标题里
   有"七天无理由"的段落，但问"我不想要了"就不一定命中。
   **保证 RAG 全链路（切分→向量→检索→引用→注入）可跑可测**，
   等配好 key 换成真 embedding 即可，接口形状不变。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

LOCAL_DIM = 512          # 本地降级向量维度
NGRAM_SIZES = (1, 2, 3)  # 中文用 1~3 元字符片段（中文里 n-gram 比分词更省事）


class EmbeddingProvider(Protocol):
    """向量化接口（换供应商/换模型的唯一契约）。

    实现方必须保证：**同一段文本每次算出来的向量一致，且已 L2 归一化**
    （检索侧直接拿点积当余弦用，见 vector_store.search）。
    """

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。批量而不是单条：知识入库时一次要算几十上百段。"""
        ...


# ======================================================================
# 1. OpenAI 兼容 embedding
# ======================================================================
class OpenAICompatibleEmbeddings:
    """调用 OpenAI 兼容的 /embeddings 接口。"""

    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        from langchain_openai import OpenAIEmbeddings

        self.name = model
        self._client = OpenAIEmbeddings(
            api_key=api_key,
            base_url=base_url or None,
            model=model,
            check_embedding_ctx_length=False,   # 兼容非 OpenAI 的第三方实现
        )
        # 维度先按配置，没配就让第一次调用时探测
        self.dim = settings.embedding_dim or 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        """调一次远端接口算完所有文本，顺手把真实维度记下来。

        `check_embedding_ctx_length=False` 是必须的：LangChain 默认会先做
        tiktoken 分词再截断，而 tiktoken 需要联网下载词表 —— 用第三方
        OpenAI 兼容服务时既没必要又会失败。

        维度**以接口真正返回的为准**（第一次调用时记下，之后不再变）：
        `EMBEDDING_DIM` 只是"没调用前的一个提示"，配错不该让诊断信息跟着撒谎 ——
        例如配成 96、模型实际返回 1024 时，若信了配置，日志与健康检查都会报一个假维度。
        （检索本身从来不信它：向量库是按**向量实际长度**建集合的，见 qdrant_store.py。）
        """
        vectors = self._client.embed_documents(texts)
        if vectors:
            self.dim = len(vectors[0])
        return [list(map(float, vector)) for vector in vectors]


# ======================================================================
# 2. 本地降级：哈希向量
# ======================================================================
class LocalHashEmbeddings:
    """字符 n-gram 哈希向量（无需网络、无需模型文件）。"""

    name = "local-hash-ngram"

    def __init__(self, dim: int = LOCAL_DIM) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """逐条算哈希向量（本地实现，无网络调用）。"""
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        """把一段文本变成 L2 归一化的哈希向量。

        原理是字符 n-gram + 哈希到固定维桶，所以它捕捉的是**词法重合**、
        不是语义 —— 这决定了它的使用边界（README 的"已知取舍"里写了）：
        同一个意思换个说法就命中不稳，用来跑通/验收 RAG 链路完全够，
        真正上线要配 EMBEDDING_API_KEY 换成真 embedding（不用改代码）。

        两个实现细节不能动：
            · 桶位置用 **md5** 而不是内置 hash()：内置 hash 对 str 带随机种子，
              同一个词在不同进程会落到不同桶 —— 存进库的向量第二天就失效了。
            · 必须 L2 归一化：检索侧直接拿点积当余弦用。
        """
        vector = [0.0] * self.dim
        cleaned = re.sub(r"\s+", "", text or "")
        if not cleaned:
            return vector

        for size in NGRAM_SIZES:
            for index in range(len(cleaned) - size + 1):
                gram = cleaned[index : index + size]
                # 用 md5 取稳定的桶位置（Python 内置 hash 每次进程都变，不能持久化）
                digest = hashlib.md5(gram.encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[bucket] += sign

        # L2 归一化：之后余弦相似度就是点积
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


# ======================================================================
# 工厂
# ======================================================================
_provider: EmbeddingProvider | None = None


def get_embeddings() -> EmbeddingProvider:
    """取得当前生效的向量化实现（进程内缓存）。"""
    global _provider
    if _provider is not None:
        return _provider

    if settings.embedding_configured:
        logger.info(
            "RAG 使用远程 embedding：model=%s base_url=%s",
            settings.embedding_model,
            settings.embedding_base_url,
        )
        _provider = OpenAICompatibleEmbeddings(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
        )
    else:
        logger.warning(
            "未配置 EMBEDDING_API_KEY：RAG 使用本地哈希向量（词法相似度，语义效果弱）。"
            "配好 key 后重启即自动切换。"
        )
        _provider = LocalHashEmbeddings()

    return _provider


def embedding_dim() -> int:
    """当前向量维度（健康检查/诊断用）。

    本地实现拿不到远程维度时就报 `LOCAL_DIM`，让调用方有个确定的数可用 ——
    比返回 0 再让上层去猜要好。
    """
    return get_embeddings().dim or LOCAL_DIM
