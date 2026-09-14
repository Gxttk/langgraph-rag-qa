"""生成模块：结合检索结果与对话历史生成回答，并标注来源溯源。"""
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate

from .config import settings
from .utils.llm import get_llm


def format_chat_history(messages: Optional[List[Any]]) -> str:
    """把最近若干条消息格式化为文本，供问题补全 / 生成时理解上下文。"""
    if not messages:
        return ""
    lines = []
    for m in messages[-settings.max_history_messages:]:
        role = "用户" if isinstance(m, HumanMessage) else "助手"
        lines.append(f"{role}: {m.content}")
    return "\n".join(lines)


def _format_context(hits: List[Dict[str, Any]]) -> str:
    """将检索结果格式化为带编号的上下文。"""
    parts = []
    for i, hit in enumerate(hits, 1):
        source = hit.get("source", "未知")
        chunk_idx = hit.get("chunk_index", 0)
        text = hit["text"]
        parts.append(f"[{i}] 来源: {source} (段落{chunk_idx})\n{text}")
    return "\n\n".join(parts)


def generate(question: str, hits: List[Dict[str, Any]], history: Optional[List[Any]] = None) -> Dict[str, Any]:
    """基于检索结果（并参考对话历史）生成回答，要求标注来源编号。

    Args:
        question: 用户本轮原始问题（保留原话，用于自然作答）
        hits: 检索结果列表
        history: 本轮之前的对话消息列表，用于理解指代与承接

    Returns:
        {"answer": str, "sources": List[dict]}
    """
    if not hits:
        return {
            "answer": "抱歉，知识库中没有找到与您问题相关的内容。",
            "sources": [],
        }

    context = _format_context(hits)
    history_text = format_chat_history(history)
    history_block = (
        f"对话历史（仅用于理解指代和承接，事实依据必须来自检索资料）：\n{history_text}\n\n"
        if history_text
        else ""
    )

    prompt = ChatPromptTemplate.from_template(
        """你是专业的知识库问答客服。请基于检索资料回答用户问题。

{history_block}要求：
1. 所有事实性陈述必须来自给定检索资料，禁止编造
2. 回答中引用资料时，在句末标注来源编号，如 [1]、[2]
3. 如果资料中没有相关信息，明确说明"资料中未提及"
4. 结合对话历史理解用户的指代、省略，但事实只以检索资料为准
5. 回答简洁清晰、分点论述，语气像专业客服

用户问题：{question}

检索资料：
{context}

请回答："""
    )

    result = (prompt | get_llm(settings.temp_answer)).invoke(
        {"history_block": history_block, "question": question, "context": context}
    )
    answer = result.content

    # 提取回答中引用的来源编号，回查检索片段
    cited_indices = set()
    for match in re.finditer(r"\[(\d+)\]", answer):
        idx = int(match.group(1))
        if 1 <= idx <= len(hits):
            cited_indices.add(idx)

    sources = [hits[i - 1] for i in sorted(cited_indices)]
    return {"answer": answer, "sources": sources}
