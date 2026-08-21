"""LLM 和 Embedding 工具封装。"""
import os
from typing import List

import dashscope
from dotenv import load_dotenv
from langchain_deepseek import ChatDeepSeek

load_dotenv(encoding="utf-8")

# DeepSeek LLM
_llm = None


def get_llm():
    """懒加载 DeepSeek 聊天模型。"""
    global _llm
    if _llm is None:
        _llm = ChatDeepSeek(
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            temperature=0.3,
        )
    return _llm


# DashScope Embedding
dashscope.api_key = os.getenv("DASHSCOPE_API_KEY")
EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIM = 512


def get_embeddings(texts: List[str]) -> List[List[float]]:
    """批量获取文本的 embedding 向量。

    Args:
        texts: 文本列表

    Returns:
        向量列表，每个向量维度为 EMBEDDING_DIM
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
