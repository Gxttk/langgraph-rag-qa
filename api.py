"""FastAPI 服务：把多轮 RAG 客服封装为 HTTP / SSE 接口。

端点：
  GET  /health                   健康检查（含 Milvus 连通性与文档块数）
  POST /chat                     多轮问答，SSE 流式（节点进度 + 答案 token + 来源）
  POST /chat/sync                多轮问答，普通 JSON 返回（便于 curl / Postman）
  GET  /session/{sid}/history    查看某会话的对话历史

运行：python api.py  或  uvicorn api:app --host 0.0.0.0 --port 8000，然后访问 /docs
"""
import json
import uuid
from typing import AsyncGenerator, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from src.config import settings
from src.graph import build_graph, build_initial_input
from src.utils.logger import logger

load_dotenv(encoding="utf-8")

# 全局复用同一个编译好的图（含内存 checkpointer），不同会话靠 thread_id 隔离。
# 生产环境把 InMemorySaver 换成 PostgresSaver 即可跨进程持久化，业务代码不变。
checkpointer = InMemorySaver()
graph = build_graph(checkpointer)

app = FastAPI(
    title="langgraph-rag-qa",
    version="1.0.0",
    description="基于 LangGraph 的多轮 RAG 知识库客服 API",
)

# 允许浏览器跨域访问（联调前端 / 本地页面时需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 节点进度的中文提示
NODE_LABELS = {
    "condense": "正在理解上下文…",
    "retrieve": "正在检索知识库…",
    "rerank": "正在精排资料…",
    "evaluate": "正在评估相关性…",
    "generate": "正在组织回答…",
}


class ChatRequest(BaseModel):
    """问答请求体（Pydantic 自动校验，缺 message 会返回 422）。"""
    message: str = Field(..., min_length=1, description="用户本轮消息")
    session_id: Optional[str] = Field(
        None, description="会话 ID；不传则新建，同一 ID 连续传入即维持多轮上下文"
    )


def _sse(event: str, data: dict) -> str:
    """拼一条 SSE 协议报文。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _clean_sources(sources: List[dict]) -> List[dict]:
    """清洗来源，把 numpy/向量库返回的数值类型转成可 JSON 序列化的原生类型。"""
    out = []
    for s in sources or []:
        out.append({
            "source": s.get("source"),
            "chunk_index": int(s.get("chunk_index", 0)),
            "score": float(s.get("score", 0.0)),
        })
    return out


@app.get("/health")
def health():
    """健康检查：探测服务本身与下游 Milvus 是否可用、库内有多少文档块。"""
    try:
        from src.utils.vector import count_documents
        return {"status": "ok", "milvus": True, "doc_chunks": int(count_documents())}
    except Exception as e:
        logger.warning(f"健康检查探测 Milvus 失败: {e}")
        return {"status": "degraded", "milvus": False, "doc_chunks": 0}


@app.post("/chat/sync")
def chat_sync(req: ChatRequest):
    """非流式多轮问答：整段答案一次性返回，方便脚本 / Postman 调用。"""
    sid = req.session_id or f"api-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": sid}}
    result = graph.invoke(build_initial_input(req.message), config=config)
    return {
        "session_id": sid,
        "standalone_question": result.get("standalone_question"),
        "answer": result.get("answer"),
        "sources": _clean_sources(result.get("sources", [])),
        "retrieve_count": result.get("retrieve_count"),
    }


@app.get("/session/{session_id}/history")
def session_history(session_id: str):
    """查看指定会话的完整对话历史。"""
    state = graph.get_state({"configurable": {"thread_id": session_id}})
    messages = (state.values or {}).get("messages", [])
    history = [
        {
            "role": "user" if isinstance(m, HumanMessage) else "assistant",
            "content": m.content,
        }
        for m in messages
    ]
    return {"session_id": session_id, "turns": len(history) // 2, "history": history}


@app.post("/chat")
async def chat_stream(req: ChatRequest):
    """流式多轮问答：SSE 依次推送 会话ID → 节点进度 → 答案 token → 来源 → 结束。"""
    sid = req.session_id or f"api-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": sid}}

    async def event_gen() -> AsyncGenerator[str, None]:
        yield _sse("session", {"session_id": sid})
        seen_nodes = set()
        try:
            async for event in graph.astream_events(
                build_initial_input(req.message), config=config, version="v2"
            ):
                kind = event["event"]
                node = event.get("metadata", {}).get("langgraph_node")

                # 节点开始 → 推一次进度
                if (
                    kind == "on_chain_start"
                    and node
                    and node not in seen_nodes
                    and node in NODE_LABELS
                ):
                    seen_nodes.add(node)
                    yield _sse("status", {"node": node, "label": NODE_LABELS[node]})

                # 只把最终生成节点的 LLM token 流式吐给用户，其余节点的 LLM 调用不外显
                elif kind == "on_chat_model_stream" and node == "generate":
                    token = event["data"]["chunk"].content
                    if token:
                        yield _sse("token", {"text": token})

            # 流结束后从 checkpointer 取最终状态，补来源与元信息
            values = (await graph.aget_state(config)).values
            yield _sse("sources", {"sources": _clean_sources(values.get("sources", []))})
            yield _sse("metadata", {
                "standalone_question": values.get("standalone_question"),
                "retrieve_count": values.get("retrieve_count"),
            })
            yield _sse("done", {})
        except Exception as e:
            logger.exception("SSE 流式处理失败")
            yield _sse("error", {"message": f"服务内部错误: {e}"})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Session-Id": sid, "Connection": "keep-alive"},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """兜底异常处理：服务端错误统一返回 500 JSON，不向客户端泄漏堆栈。"""
    logger.exception(f"未处理异常: {request.url}")
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请查看服务日志"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
