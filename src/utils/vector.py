"""Milvus 向量库连接与检索封装。"""
import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, MilvusClient, connections

load_dotenv(encoding="utf-8")

COLLECTION_NAME = os.getenv("MILVUS_COLLECTION", "langgraph_rag_kb")
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")

_client: MilvusClient | None = None


def get_client() -> MilvusClient:
    """懒加载 Milvus 客户端。"""
    global _client
    if _client is None:
        _client = MilvusClient(uri=f"http://{MILVUS_HOST}:{MILVUS_PORT}")
    return _client


def ensure_collection():
    """确保 collection 存在，不存在则创建（幂等）。"""
    client = get_client()
    if client.has_collection(COLLECTION_NAME):
        return

    schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
    schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=512)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field("source", DataType.VARCHAR, max_length=500)
    schema.add_field("chunk_index", DataType.INT64)

    index_params = client.prepare_index_params()
    index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")

    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=schema,
        index_params=index_params,
    )
    print(f"[vector] 创建 collection: {COLLECTION_NAME}")


def insert_chunks(chunks: List[Dict[str, Any]]):
    """批量插入文本块。

    Args:
        chunks: [{"text": str, "vector": List[float], "source": str, "chunk_index": int}, ...]
    """
    if not chunks:
        return
    client = get_client()
    ensure_collection()
    client.insert(collection_name=COLLECTION_NAME, data=chunks)
    print(f"[vector] 插入 {len(chunks)} 条文本块")


def search(vector: List[float], top_k: int = 4) -> List[Dict[str, Any]]:
    """向量检索，返回最相关的文本块。

    Args:
        vector: 查询向量
        top_k: 返回条数

    Returns:
        [{"text": str, "source": str, "chunk_index": int, "score": float}, ...]
    """
    client = get_client()
    ensure_collection()
    results = client.search(
        collection_name=COLLECTION_NAME,
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
    if not client.has_collection(COLLECTION_NAME):
        return 0
    stats = client.get_collection_stats(COLLECTION_NAME)
    return stats.get("row_count", 0)


def clear_collection():
    """清空 collection（删除后重建），避免重复入库。"""
    client = get_client()
    if client.has_collection(COLLECTION_NAME):
        client.drop_collection(COLLECTION_NAME)
        print(f"[vector] 已清空旧 collection: {COLLECTION_NAME}")
