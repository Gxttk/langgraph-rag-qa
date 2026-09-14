"""LLM 和 Embedding 工具封装。"""
from typing import Dict, List

import dashscope
from langchain_deepseek import ChatDeepSeek

from ..config import settings

# 模块导入即配置好 DashScope
dashscope.api_key = settings.dashscope_api_key
EMBEDDING_MODEL = settings.embedding_model
EMBEDDING_DIM = settings.embedding_dim

# 按温度缓存实例：同一温度复用同一客户端，不同节点用不同温度互不影响
_llm_cache: Dict[float, ChatDeepSeek] = {}


def get_llm(temperature: float = settings.temp_answer) -> ChatDeepSeek:
    """懒加载 DeepSeek 聊天模型，按温度分别缓存。

    Args:
        temperature: 采样温度，调用方按节点用途传 settings.temp_* 对应值
    """
    if temperature not in _llm_cache:
        _llm_cache[temperature] = ChatDeepSeek(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            temperature=temperature,
        )
    return _llm_cache[temperature]


def get_embeddings(texts: List[str]) -> List[List[float]]:
    """批量获取文本的 embedding 向量。

    Args:
        texts: 文本列表

    Returns:
        向量列表，每个向量维度为 settings.embedding_dim
    """
    if not texts:
        return []
    resp = dashscope.TextEmbedding.call(
        model=EMBEDDING_MODEL,
        input=texts,
        dimension=EMBEDDING_DIM,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Embedding 调用失败: {resp.code} - {resp.message}")
    return [item["embedding"] for item in resp.output["embeddings"]]
