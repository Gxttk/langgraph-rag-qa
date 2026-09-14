"""交互式多轮客服（命令行）。

同一 session 内连续提问，能理解"它/这个/上面那个"等指代；
底层用 InMemorySaver 按 thread_id 保存对话历史，进程退出即清空
（生产环境换成 PostgresSaver 即可跨进程持久化，图代码无需改动）。

用法：
  python chat.py
指令：输入 "新会话" 清空当前对话重新开始，输入 "退出" 结束。
"""
import sys
import uuid

# Windows 控制台 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv(encoding="utf-8")

from langgraph.checkpoint.memory import InMemorySaver

from src.graph import build_graph, build_initial_input


def main():
    # 关键：整个会话复用同一个 graph + checkpointer，历史才能跨多轮保留
    checkpointer = InMemorySaver()
    graph = build_graph(checkpointer)

    session_id = f"cli-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": session_id}}

    print("=" * 60)
    print("知识库客服已启动（输入「退出」结束，「新会话」清空重来）")
    print(f"当前会话 ID：{session_id}")
    print("=" * 60)

    while True:
        try:
            question = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not question:
            continue
        if question in ("退出", "quit", "exit", "q"):
            break
        if question == "新会话":
            session_id = f"cli-{uuid.uuid4().hex[:8]}"
            config = {"configurable": {"thread_id": session_id}}
            print(f"[已开启新会话 {session_id}]")
            continue

        result = graph.invoke(build_initial_input(question), config=config)

        print("\n客服：", result["answer"])
        if result.get("sources"):
            srcs = "；".join(
                f"{s['source']}#段落{s['chunk_index']}({s['score']:.3f})" for s in result["sources"]
            )
            print(f"[来源] {srcs}")

    print("\n已退出，再见。")


if __name__ == "__main__":
    main()
