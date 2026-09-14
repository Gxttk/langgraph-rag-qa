"""Milvus 向量库连接与检索封装。"""
from typing import Any, Dict, List

from pymilvus import DataType, MilvusClient

from ..config import settings
from .logger import logger

_client: MilvusClient | None = None


def get_client() -> MilvusClient:
    """懒加载 Milvus 客户端（单例）。"""
    global _client
    if _client is None:
        _client = MilvusClient(uri=settings.milvus_uri)
    return _client


def ensure_collection():
    """确保 collection 存在，不存在则创建（幂等）。向量维度从配置读取，只此一处定义。"""
    client = get_client()
    if client.has_collection(settings.collection_name):
        return

    dim = settings.embedding_dim
    schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
    schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field("source", DataType.VARCHAR, max_length=500)
    schema.add_field("chunk_index", DataType.INT64)

    index_params = client.prepare_index_params()
    index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")

    client.create_collection(
        collection_name=settings.collection_name,
        schema=schema,
        index_params=index_params,
    )
    logger.info(f"创建 collection: {settings.collection_name}（dim={dim}）")


def insert_chunks(chunks: List[Dict[str, Any]]):
    """批量插入文本块。"""
    if not chunks:
        return
    client = get_client()
    ensure_collection()
    client.insert(collection_name=settings.collection_name, data=chunks)
    logger.debug(f"插入 {len(chunks)} 条文本块")


def search(vector: List[float], top_k: int = settings.retrieve_top_k) -> List[Dict[str, Any]]:
    """向量检索，返回最相关的文本块。"""
    client = get_client()
    ensure_collection()
    results = client.search(
        collection_name=settings.collection_name,
        data=[vector],
        limit=top_k,
        output_fields=["text", "source", "chunk_index"],
        search_params={"metric_type": "COSINE"},
    )
    hits = []
    for item in results[0]:
        entity = item["entity"]
        hits.append({
            "text": entity["text"],
            "source": entity["source"],
            "chunk_index": entity["chunk_index"],
            "score": item["distance"],
        })
    return hits


def count_documents() -> int:
    """返回 collection 中的文档数量。"""
    client = get_client()
    if not client.has_collection(settings.collection_name):
        return 0
    stats = client.get_collection_stats(settings.collection_name)
    return stats.get("row_count", 0)


def clear_collection():
    """清空 collection（删除后重建），避免重复入库。"""
    client = get_client()
    if client.has_collection(settings.collection_name):
        client.drop_collection(settings.collection_name)
        logger.info(f"已清空旧 collection: {settings.collection_name}")
