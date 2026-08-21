"""检索模块：查询改写 + 向量检索 + 结果去重。"""
import json
import os
import re
from typing import Any, Dict, List

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate

from .utils.llm import get_embeddings, get_llm
from .utils.vector import search

load_dotenv(encoding="utf-8")

RETRIEVE_TOP_K = int(os.getenv("RETRIEVE_TOP_K", "4"))
REWRITE_NUM_QUERIES = int(os.getenv("REWRITE_NUM_QUERIES", "3"))


def _clean_json(text: str) -> str:
    """去掉 markdown 代码块包裹。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return text


def rewrite_query(question: str, feedback: str = "") -> List[str]:
    """查询改写：将用户口语化提问扩写为多个专业查询词。

    Args:
        question: 用户原始问题
        feedback: 上一轮检索质量评估的反馈（重检时传入，指导改写方向）

    Returns:
        改写后的查询词列表
    """
    feedback_section = ""
    if feedback:
        feedback_section = f"\n上一轮检索评估反馈：{feedback}\n请根据反馈针对性地调整查询词，补充缺失的信息角度。"

    prompt = ChatPromptTemplate.from_template(
        """你是检索优化专家。请将用户问题「{question}」改写为 {n} 个更适合向量检索的专业查询词。
要求：
- 每个查询词独立、具体，覆盖不同角度
- 用专业术语替代口语化表达
- 直接输出 JSON 数组字符串，形如 ["查询1", "查询2"]{feedback_section}

请输出："""
    )
    try:
        result = (prompt | get_llm()).invoke(
            {"question": question, "n": REWRITE_NUM_QUERIES, "feedback_section": feedback_section}
        )
        text = _clean_json(result.content)
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            text = text[start : end + 1]
        queries = json.loads(text)
        if isinstance(queries, list):
            return [str(q) for q in queries if str(q).strip()]
    except Exception as e:
        print(f"[retriever] 查询改写失败，使用原始问题: {e}")
    return [question]


def retrieve(question: str, top_k: int = RETRIEVE_TOP_K, feedback: str = "") -> tuple:
    """查询改写 + 批量向量检索 + 去重合并。

    Args:
        question: 用户问题
        top_k: 每个查询词返回的条数
        feedback: 上一轮评估反馈（重检时传入）

    Returns:
        (hits, has_error): 检索结果列表, 是否发生检索异常
    """
    queries = rewrite_query(question, feedback=feedback)
    print(f"[retriever] 改写为 {len(queries)} 个查询: {queries}")

    # 批量 embedding，减少 API 调用次数
    try:
        vectors = get_embeddings(queries)
    except Exception as e:
        print(f"[retriever] embedding 失败: {e}")
        return [], True

    all_hits = []
    seen_texts = set()
    success_count = 0

    for q, vector in zip(queries, vectors):
        try:
            hits = search(vector, top_k=top_k)
            success_count += 1
        except Exception as e:
            print(f"[retriever] 检索失败（已跳过）: {q[:30]}... 错误: {e}")
            continue
        for hit in hits:
            if hit["text"] not in seen_texts:
                seen_texts.add(hit["text"])
                all_hits.append(hit)

    all_hits.sort(key=lambda x: x["score"], reverse=True)
    print(f"[retriever] 去重后共 {len(all_hits)} 条结果（成功检索 {success_count}/{len(queries)} 个查询）")

    # 全部查询都失败 → 标记为检索异常
    has_error = (success_count == 0)
    return all_hits, has_error


def rerank(question: str, hits: List[Dict[str, Any]], top_k: int = RETRIEVE_TOP_K) -> List[Dict[str, Any]]:
    """LLM 重排序：根据问题对检索结果重新打分排序，取 top-k。

    两阶段检索：
    - 第一阶段（粗排）：向量相似度快速召回
    - 第二阶段（精排/Rerank）：LLM 细致判断相关性，重新排序

    Args:
        question: 用户问题
        hits: 粗排检索结果
        top_k: 重排后保留的条数

    Returns:
        重排后的检索结果
    """
    if not hits:
        return []
    if len(hits) <= top_k:
        return hits  # 结果不够 top_k，不需要重排

    # 格式化检索结果，带编号
    items_text = []
    for i, h in enumerate(hits, 1):
        items_text.append(f"[{i}] {h['text'][:200]}")

    prompt = ChatPromptTemplate.from_template(
        """你是检索重排序专家。请根据用户问题，对以下检索结果按相关性从高到低排序。

要求：
- 只输出编号列表，形如 [3, 1, 4, 2]
- 最相关的排最前面
- 不要输出其他内容

用户问题：{question}

检索结果：
{items}

请输出排序后的编号列表："""
    )

    try:
        result = (prompt | get_llm()).invoke({
            "question": question,
            "items": "\n".join(items_text),
        })
        text = _clean_json(result.content)
        # 提取编号列表
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            text = text[start : end + 1]
        ranked_indices = json.loads(text)

        # 按编号重新排序，过滤无效编号
        ranked_hits = []
        seen = set()
        for idx in ranked_indices:
            if isinstance(idx, int) and 1 <= idx <= len(hits) and idx not in seen:
                seen.add(idx)
                ranked_hits.append(hits[idx - 1])

        # 补充 LLM 漏掉的结果
        for i, h in enumerate(hits):
            if (i + 1) not in seen:
                ranked_hits.append(h)

        print(f"[rerank] 重排完成，保留 top-{top_k}")
        return ranked_hits[:top_k]

    except Exception as e:
        print(f"[rerank] 重排失败，使用原始排序: {e}")
        return hits[:top_k]
