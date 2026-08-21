"""LangGraph 编排：检索 → 质量评估 →（不合格则重检）→ 生成回答。

单循环设计：检索完成后评估召回片段与问题的相关性，
相关性过低则根据评估反馈改写查询重新检索，最多重试 MAX_RETRIEVE_RETRIES 次。
"""
import json
import os
import re
from typing import Any, Dict, List, TypedDict

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph

from .generator import generate
from .retriever import rerank, retrieve
from .utils.llm import get_llm

load_dotenv(encoding="utf-8")

MAX_RETRIEVE_RETRIES = int(os.getenv("MAX_RETRIEVE_RETRIES", "2"))


class RAGState(TypedDict):
    """RAG 工作流状态。"""
    question: str
    retrieve_count: int
    hits: List[Dict[str, Any]]
    is_relevant: bool
    feedback: str
    answer: str
    sources: List[Dict[str, Any]]
    retrieve_error: bool  # 检索是否发生异常（Milvus未启动等），用于区分"服务不可用"和"没匹配到内容"


def retrieve_node(state: RAGState) -> Dict[str, Any]:
    """检索节点：查询改写 + 向量检索 + 去重，重检时合并旧结果。"""
    question = state["question"]
    count = state.get("retrieve_count", 0)
    feedback = state.get("feedback", "")
    old_hits = state.get("hits", [])

    print(f"\n[retrieve] 第 {count + 1} 轮检索...")

    try:
        new_hits, has_error = retrieve(question, feedback=feedback)
    except Exception as e:
        print(f"[retrieve] 检索失败: {e}")
        new_hits, has_error = [], True

    # 合并旧结果和新结果，按文本去重
    seen_texts = {h["text"] for h in old_hits}
    merged = list(old_hits)
    for hit in new_hits:
        if hit["text"] not in seen_texts:
            seen_texts.add(hit["text"])
            merged.append(hit)

    # 按相似度重新排序
    merged.sort(key=lambda x: x["score"], reverse=True)
    return {"hits": merged, "retrieve_count": count + 1, "retrieve_error": has_error}


def rerank_node(state: RAGState) -> Dict[str, Any]:
    """重排序节点：LLM 对检索结果重新打分排序，取 top-k。

    两阶段检索：粗排（向量相似度）→ 精排（LLM 重排序）
    检索异常时跳过重排，直接返回原结果。
    """
    if state.get("retrieve_error") or not state.get("hits"):
        return {}  # 检索异常或无结果，跳过重排

    print("[rerank] LLM 重排序中...")
    reranked = rerank(state["question"], state["hits"])
    return {"hits": reranked}


def evaluate_node(state: RAGState) -> Dict[str, Any]:
    """检索质量评估：判断召回片段是否与问题相关。"""
    question = state["question"]
    hits = state["hits"]
    count = state.get("retrieve_count", 0)

    if not hits:
        return {"is_relevant": False, "feedback": "未检索到任何内容"}

    # 拼接 top-3 片段做评估
    sample = "\n\n".join([h["text"][:300] for h in hits[:3]])

    prompt = ChatPromptTemplate.from_template(
        """请评估以下检索片段是否能回答用户问题。

用户问题：{question}

检索片段：
{sample}

请严格按 JSON 输出：
- is_relevant: bool，片段能回答问题为 true，否则为 false
- feedback: string，若不相关，说明缺什么信息、应该检索什么方向"""
    )

    try:
        result = (prompt | get_llm()).invoke({"question": question, "sample": sample})
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.content.strip(), flags=re.MULTILINE)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]
        parsed = json.loads(text)
        is_relevant = bool(parsed.get("is_relevant", True))
        feedback = parsed.get("feedback", "")
    except Exception as e:
        print(f"[evaluate] 评估失败，默认通过: {e}")
        is_relevant = True
        feedback = ""

    print(f"[evaluate] 相关性: {'通过' if is_relevant else '不通过'} {feedback}")

    # 达到最大重试次数强制通过
    if count >= MAX_RETRIEVE_RETRIES:
        print(f"[evaluate] 已达最大重试次数 {MAX_RETRIEVE_RETRIES}，强制通过")
        is_relevant = True

    return {"is_relevant": is_relevant, "feedback": feedback}


def generate_node(state: RAGState) -> Dict[str, Any]:
    """生成节点：基于检索结果生成回答 + 来源溯源。

    区分两种空结果：
    - 检索异常（Milvus未启动等）→ 明确提示服务不可用
    - 检索成功但没匹配 → 提示知识库无相关内容
    """
    print("[generate] 生成回答...")

    # 检索异常：不调用 LLM，直接返回服务不可用提示
    if state.get("retrieve_error"):
        return {
            "answer": "检索服务暂时不可用（Milvus 连接失败），请检查向量库服务是否启动，或稍后重试。",
            "sources": [],
        }

    try:
        result = generate(state["question"], state["hits"])
    except Exception as e:
        print(f"[generate] 生成失败: {e}")
        result = {
            "answer": f"回答生成失败（{e}），请稍后重试。",
            "sources": [],
        }
    return {"answer": result["answer"], "sources": result["sources"]}


def should_retry(state: RAGState) -> str:
    """条件边：评估不通过且未达上限则重检，否则生成回答。

    优化：
    - 检索异常（Milvus未启动等）→ 直接进 generate，不浪费 LLM 重检
    - 第一次检索就全空 → 直接进 generate（大概率是异常，后续加相似度阈值后也可能是正常空）
    """
    # 检索异常 → 直接兜底，不重检
    if state.get("retrieve_error"):
        return "generate"
    # 第一次检索就全空 → 直接兜底
    if not state.get("hits") and state.get("retrieve_count", 0) <= 1:
        return "generate"
    # 正常情况：评估不通过且未达上限 → 重检
    if not state.get("is_relevant", True) and state.get("retrieve_count", 0) < MAX_RETRIEVE_RETRIES:
        return "retrieve"
    return "generate"


def build_graph():
    """构建并编译 RAG 工作流图。

    流程：retrieve（粗排）→ rerank（精排）→ evaluate（质量评估）→ generate（生成+溯源）
    """
    builder = StateGraph(RAGState)

    builder.add_node("retrieve", retrieve_node)
    builder.add_node("rerank", rerank_node)
    builder.add_node("evaluate", evaluate_node)
    builder.add_node("generate", generate_node)

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "evaluate")
    builder.add_conditional_edges("evaluate", should_retry)
    builder.add_edge("generate", END)

    return builder.compile()
