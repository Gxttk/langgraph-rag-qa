"""全局集中配置。

所有环境变量与可调超参统一在此读取，其余模块只允许 `from src.config import settings`，
不再各自 os.getenv，避免配置散落、同一常量（如向量维度）在多处硬编码导致改一漏一。
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv(encoding="utf-8")


def _env(key: str, default, cast=str):
    """读取环境变量；缺省或为空串时回退默认值，并按 cast 转换类型。"""
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return default
    return cast(value)


@dataclass(frozen=True)
class Settings:
    # ---------- DeepSeek 大模型 ----------
    deepseek_api_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY", ""))
    deepseek_model: str = field(default_factory=lambda: _env("DEEPSEEK_MODEL", "deepseek-chat"))
    deepseek_base_url: str = field(default_factory=lambda: _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))

    # ---------- DashScope Embedding ----------
    dashscope_api_key: str = field(default_factory=lambda: _env("DASHSCOPE_API_KEY", ""))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "text-embedding-v3"))
    embedding_dim: int = field(default_factory=lambda: _env("EMBEDDING_DIM", 512, int))

    # ---------- Milvus 向量库 ----------
    milvus_host: str = field(default_factory=lambda: _env("MILVUS_HOST", "localhost"))
    milvus_port: str = field(default_factory=lambda: _env("MILVUS_PORT", "19530"))
    collection_name: str = field(default_factory=lambda: _env("MILVUS_COLLECTION", "langgraph_rag_kb"))

    # ---------- 文档分块 ----------
    chunk_size: int = field(default_factory=lambda: _env("CHUNK_SIZE", 500, int))
    chunk_overlap: int = field(default_factory=lambda: _env("CHUNK_OVERLAP", 50, int))
    embed_batch_size: int = field(default_factory=lambda: _env("EMBED_BATCH_SIZE", 10, int))
    min_chunk_len: int = field(default_factory=lambda: _env("MIN_CHUNK_LEN", 20, int))

    # ---------- 检索 ----------
    retrieve_top_k: int = field(default_factory=lambda: _env("RETRIEVE_TOP_K", 4, int))
    rewrite_num_queries: int = field(default_factory=lambda: _env("REWRITE_NUM_QUERIES", 3, int))
    max_retrieve_retries: int = field(default_factory=lambda: _env("MAX_RETRIEVE_RETRIES", 2, int))

    # ---------- 分节点温度（越低越确定）----------
    temp_rerank: float = field(default_factory=lambda: _env("TEMP_RERANK", 0.0, float))
    temp_eval: float = field(default_factory=lambda: _env("TEMP_EVAL", 0.1, float))
    temp_answer: float = field(default_factory=lambda: _env("TEMP_ANSWER", 0.2, float))
    temp_rewrite: float = field(default_factory=lambda: _env("TEMP_REWRITE", 0.5, float))
    temp_condense: float = field(default_factory=lambda: _env("TEMP_CONDENSE", 0.1, float))

    # ---------- 多轮会话 ----------
    # 问题补全 / 生成时携带的最近历史消息条数（6 条 ≈ 3 轮），用于控制 token
    max_history_messages: int = field(default_factory=lambda: _env("MAX_HISTORY_MESSAGES", 6, int))

    # ---------- 服务与日志 ----------
    api_host: str = field(default_factory=lambda: _env("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: _env("API_PORT", 8000, int))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    @property
    def milvus_uri(self) -> str:
        return f"http://{self.milvus_host}:{self.milvus_port}"


settings = Settings()
