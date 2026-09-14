"""LangGraph 多轮 RAG 编排。

一轮内流程：
    condense（结合历史把残句补全为独立问题）
      → retrieve（用独立问题做查询改写+向量检索）
      → rerank（LLM 精排）
      → evaluate（相关性评估，不达标带反馈重检）
      → generate（用用户原话+历史生成，写回消息历史）

会话记忆：State.messages 用 add_messages reducer 累积，配合 checkpointer 按
thread_id（session_id）持久化；检索/精排/评估用补全后的独立问题以保证召回，
生成用用户原始问题+历史以保证回答自然、能承接指代。
"""
import json
import re
from typing import Annotated, Any, Dict, List, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .config import settings
from .generator import format_chat_history, generate
from .retriever import rerank, retrieve
from .utils.llm import get_llm
from .utils.logger import logger


class RAGState(TypedDict):
    """RAG 工作流状态。"""
    # 会话级：消息历史，add_messages 跨轮自动累积
    messages: Annotated[List[AnyMessage], add_messages]
    # 本轮工作字段
    question: str            # 用户本轮原始问题
    standalone_question: str  # 补全后的独立检索问题
    retrieve_count: int
    hits: List[Dict[str, Any]]
    is_relevant: bool
    feedback: str
    answer: str
    sources: List[Dict[str, Any]]
    retrieve_error: bool


def build_initial_input(question: str) -> Dict[str, Any]:
    """构造一轮的初始输入：本轮用户消息入历史，同时重置所有单轮工作字段。

    多轮场景下每次 invoke 都调用它，避免 checkpointer 中残留上一轮的 hits/计数。
    """
    return {
        "messages": [HumanMessage(content=question)],
        "question": question,
        "standalone_question": "",
        "retrieve_count": 0,
        "hits": [],
        "is_relevant": False,
        "feedback": "",
        "answer": "",
        "sources": [],
        "retrieve_error": False,
    }


def condense_node(state: RAGState) -> Dict[str, Any]:
    """问题补全（指代消解）：结合历史把残句改写为脱离上下文也能懂的独立问题。

    首轮（无历史）直接使用原问题，不额外调用 LLM，省一次请求。
    """
    question = state["question"]
    prior_history = state.get("messages", [])[:-1]  # 最后一条是本轮 HumanMessage

    if not prior_history:
        return {"standalone_question": question}

    history_text = format_chat_history(prior_history)
    prompt = ChatPromptTemplate.from_template(
        """你是多轮对话的问题补全助手。请结合历史对话，把用户"当前问题"改写为
不依赖上下文也能完整理解的独立问题：消解"它/这个/上面那个"等指代、补全省略成分。
若当前问题本身已经完整，则原样输出。

只输出补全后的问题本身，不要解释、不要引号、不要前缀。

历史对话：
{history}

当前问题：{question}

补全后的独立问题："""
    )
    try:
        result = (prompt | get_llm(settings.temp_condense)).invoke(
            {"history": history_text, "question": question}
        )
        standalone = result.content.strip().strip("\"'“”").strip()
        if not standalone:
            standalone = question
    except Exception as e:
        logger.warning(f"问题补全失败，回退原问题: {e}")
        standalone = question

    if standalone != question:
        logger.info(f"问题补全: 「{question}」→「{standalone}」")
    return {"standalone_question": standalone}


def retrieve_node(state: RAGState) -> Dict[str, Any]:
    """检索节点：用补全后的独立问题做查询改写 + 向量检索 + 去重，重检合并旧结果。"""
    search_query = state.get("standalone_question") or state["question"]
    count = state.get("retrieve_count", 0)
    feedback = state.get("feedback", "")
    old_hits = state.get("hits", [])

    logger.info(f"第 {count + 1} 轮检索（检索问题：{search_query}）")

    try:
        new_hits, has_error = retrieve(search_query, feedback=feedback)
    except Exception as e:
        logger.error(f"检索节点异常: {e}")
        new_hits, has_error = [], True

    seen_texts = {h["text"] for h in old_hits}
    merged = list(old_hits)
    for hit in new_hits:
        if hit["text"] not in seen_texts:
            seen_texts.add(hit["text"])
            merged.append(hit)

    merged.sort(key=lambda x: x["score"], reverse=True)
    return {"hits": merged, "retrieve_count": count + 1, "retrieve_error": has_error}


def rerank_node(state: RAGState) -> Dict[str, Any]:
    """精排节点：LLM 对粗排结果重排取 top-k；异常或无结果时跳过。"""
    if state.get("retrieve_error") or not state.get("hits"):
        return {}
    search_query = state.get("standalone_question") or state["question"]
    reranked = rerank(search_query, state["hits"])
    return {"hits": reranked}


def evaluate_node(state: RAGState) -> Dict[str, Any]:
    """检索质量评估：判断召回片段是否能回答（补全后的）问题。"""
    search_query = state.get("standalone_question") or state["question"]
    hits = state["hits"]
    count = state.get("retrieve_count", 0)

    if not hits:
        return {"is_relevant": False, "feedback": "未检索到任何内容"}

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
        result = (prompt | get_llm(settings.temp_eval)).invoke({"question": search_query, "sample": sample})
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.content.strip(), flags=re.MULTILINE)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]
        parsed = json.loads(text)
        is_relevant = bool(parsed.get("is_relevant", True))
        feedback = parsed.get("feedback", "")
    except Exception as e:
        logger.warning(f"评估失败，默认通过: {e}")
        is_relevant = True
        feedback = ""

    logger.info(f"相关性评估: {'通过' if is_relevant else '不通过'} {feedback}")

    if count >= settings.max_retrieve_retries:
        logger.info(f"已达最大重检次数 {settings.max_retrieve_retries}，强制通过")
        is_relevant = True

    return {"is_relevant": is_relevant, "feedback": feedback}


def generate_node(state: RAGState) -> Dict[str, Any]:
    """生成节点：用用户原话 + 历史生成回答、做来源溯源，并把本轮问答写回历史。"""
    prior_history = state.get("messages", [])[:-1]

    if state.get("retrieve_error"):
        answer = "检索服务暂时不可用（向量库连接失败），请检查向量库是否启动，或稍后重试。"
        sources = []
    else:
        try:
            result = generate(state["question"], state["hits"], history=prior_history)
            answer, sources = result["answer"], result["sources"]
        except Exception as e:
            logger.error(f"生成失败: {e}")
            answer, sources = f"回答生成失败（{e}），请稍后重试。", []

    # 写回本轮 AI 消息（Human 消息已在 build_initial_input 时入历史）
    return {"answer": answer, "sources": sources, "messages": [AIMessage(content=answer)]}


def should_retry(state: RAGState) -> str:
    """条件边：评估不通过且未达上限则重检，否则生成。"""
    if state.get("retrieve_error"):
        return "generate"
    if not state.get("hits") and state.get("retrieve_count", 0) <= 1:
        return "generate"
    if not state.get("is_relevant", True) and state.get("retrieve_count", 0) < settings.max_retrieve_retries:
        return "retrieve"
    return "generate"


def build_graph(checkpointer=None):
    """构建并编译多轮 RAG 图；注入 checkpointer 即可按 thread_id 维持多轮会话。"""
    builder = StateGraph(RAGState)

    builder.add_node("condense", condense_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("rerank", rerank_node)
    builder.add_node("evaluate", evaluate_node)
    builder.add_node("generate", generate_node)

    builder.add_edge(START, "condense")
    builder.add_edge("condense", "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "evaluate")
    builder.add_conditional_edges("evaluate", should_retry)
    builder.add_edge("generate", END)

    return builder.compile(checkpointer=checkpointer)
