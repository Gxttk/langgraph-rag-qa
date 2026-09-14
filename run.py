"""RAG 知识库问答命令行入口（单轮）。

用法：
  python run.py --ingest              # 将 data/ 目录下的文档入库
  python run.py "你的问题"             # 单轮提问
多轮连续对话（客服模式）请使用 chat.py。
"""
import sys
from pathlib import Path

# Windows 控制台 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv(encoding="utf-8")

from src.ingest import ingest_directory
from src.graph import build_graph, build_initial_input


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python run.py --ingest       入库 data/ 目录下的文档")
        print('  python run.py "问题"          单轮提问（多轮对话用 python chat.py）')
        return

    arg = sys.argv[1]

    if arg == "--ingest":
        data_dir = str(Path(__file__).parent / "data")
        ingest_directory(data_dir)
        return

    # 单轮提问（不挂 checkpointer，问完即止）
    question = arg
    print(f"\n问题: {question}")
    print("=" * 60)

    graph = build_graph()
    result = graph.invoke(build_initial_input(question))

    print("\n" + "=" * 60)
    print("【回答】")
    print(result["answer"])

    if result["sources"]:
        print("\n【引用来源】")
        for i, src in enumerate(result["sources"], 1):
            print(f"  {i}. {src['source']} (段落{src['chunk_index']}) - 相似度: {src['score']:.4f}")

    print(f"\n检索轮次: {result['retrieve_count']}")


if __name__ == "__main__":
    main()
