"""生成模块：基于检索结果生成回答，并标注来源溯源。"""
from typing import Any, Dict, List

from langchain_core.prompts import ChatPromptTemplate

from .utils.llm import get_llm


def _format_context(hits: List[Dict[str, Any]]) -> str:
    """将检索结果格式化为带编号的上下文。"""
    parts = []
    for i, hit in enumerate(hits, 1):
        source = hit.get("source", "未知")
        chunk_idx = hit.get("chunk_index", 0)
        text = hit["text"]
        parts.append(f"[{i}] 来源: {source} (段落{chunk_idx})\n{text}")
    return "\n\n".join(parts)


def generate(question: str, hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """基于检索结果生成回答，要求标注来源编号。

    Args:
        question: 用户问题
        hits: 检索结果列表

    Returns:
        {"answer": str, "sources": List[dict]}
    """
    if not hits:
        return {
            "answer": "抱歉，知识库中没有找到与您问题相关的内容。",
            "sources": [],
        }

    context = _format_context(hits)

    prompt = ChatPromptTemplate.from_template(
        """你是专业的知识库问答助手。请基于以下检索资料回答用户问题。

要求：
1. 所有事实性陈述必须来自给定资料，禁止编造
2. 回答中引用资料时，在句末标注来源编号，如 [1]、[2]
3. 如果资料中没有相关信息，明确说明"资料中未提及"
4. 回答结构清晰，分点论述

用户问题：{question}

检索资料：
{context}

请回答："""
    )

    result = (prompt | get_llm()).invoke({"question": question, "context": context})
    answer = result.content

    # 提取回答中引用的来源编号
    import re
    cited_indices = set()
    for match in re.finditer(r"\[(\d+)\]", answer):
        idx = int(match.group(1))
        if 1 <= idx <= len(hits):
            cited_indices.add(idx)

    sources = [hits[i - 1] for i in sorted(cited_indices)]

    return {"answer": answer, "sources": sources}
