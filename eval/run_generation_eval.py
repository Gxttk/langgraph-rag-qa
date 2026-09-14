"""RAG 生成层评测（第二阶段）：引用正确率 + 无答案题兜底率。

对每道题跑一遍完整 graph（condense→retrieve→rerank→evaluate→[重检]→generate），
不单独 mock 任何环节，评的是用户真正拿到的最终回答。

有答案题（has_answer=true）评三项：
  引用标注率   : 回答里至少出现一个 [n] 来源编号的题占比（有没有标来源）
  引用精确率   : 题均 |被引来源 ∩ golden| / |被引来源|（标出来的来源对不对）
  正确引用覆盖 : 至少有一个被引来源命中 golden 的题占比（有没有引到正确文档）
  （被引来源直接取 generator 回查后的 state["sources"]，与回答里的 [n] 一一对应）

无答案题（has_answer=false）评：
  正确拒答率 : 回答是否明确表达“资料中未提及/没有找到”，而不是拿着不相关片段硬答。
  用关键词规则做初判（零额外 LLM、可复现），每题完整回答落盘供人工复核。

用法（项目根目录）：python eval/run_generation_eval.py
"""
import json
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from src.config import settings  # noqa: E402
from src.graph import build_graph, build_initial_input  # noqa: E402
from src.utils.vector import count_documents  # noqa: E402
from src.utils.logger import logger  # noqa: E402

logger.remove()

# 拒答兜底的表述特征（命中任一即初判为“正确拒答”）
REJECT_PATTERN = re.compile(
    r"未提及|没有找到|没找到|没有相关|无相关|未涉及|未收录|无法提供|暂无|不包含|没有.*相关内容|未包含"
)
CITE_PATTERN = re.compile(r"\[(\d+)\]")


def snapshot():
    info = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "collection": settings.collection_name,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "total_chunks": count_documents(),
        "temp_answer": settings.temp_answer,
        "temp_eval": settings.temp_eval,
    }
    try:
        info["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        info["git_commit"] = "unknown"
    return info


def main():
    # Milvus 可达性前置检查
    try:
        with socket.create_connection((settings.milvus_host, int(settings.milvus_port)), timeout=3):
            pass
    except OSError:
        raise SystemExit(f"Milvus 不可达 {settings.milvus_host}:{settings.milvus_port}，请先 docker compose up -d standalone")

    items = [json.loads(line) for line in (ROOT / "eval" / "dataset.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    answerable = [it for it in items if it["has_answer"]]
    oos = [it for it in items if not it["has_answer"]]

    graph = build_graph(InMemorySaver())
    rows = []

    def run_one(it):
        cfg = {"configurable": {"thread_id": f"gen-eval-{it['id']}"}}
        out = graph.invoke(build_initial_input(it["question"]), config=cfg)
        answer = out.get("answer", "") or ""
        cited = [s.get("source") for s in out.get("sources", [])]
        n_marker = len(CITE_PATTERN.findall(answer))
        return out, answer, cited, n_marker

    # ---------- 有答案题：引用指标 ----------
    n_with_marker = 0
    precision_sum = 0.0
    n_cover = 0
    for it in answerable:
        out, answer, cited, n_marker = run_one(it)
        golden = set(it["golden_sources"])
        cited_set = set(cited)
        has_marker = n_marker >= 1
        if has_marker:
            n_with_marker += 1
        prec = (len(cited_set & golden) / len(cited_set)) if cited_set else 0.0
        cover = 1 if (cited_set & golden) else 0
        n_cover += cover
        precision_sum += prec
        rows.append({
            "id": it["id"], "has_answer": True, "question": it["question"],
            "golden": it["golden_sources"], "cited_sources": cited,
            "n_marker": n_marker, "cite_precision": round(prec, 3), "cover": cover,
            "retrieve_count": out.get("retrieve_count"), "answer": answer,
        })
        print(f"[引用] Q{it['id']:>2} 引用{n_marker}个 精确率{prec:.2f} {'命中' if cover else '未命中'}")

    n_a = len(answerable)
    cite_marker_rate = n_with_marker / n_a
    cite_precision = precision_sum / n_a
    cite_cover_rate = n_cover / n_a

    # ---------- 无答案题：兜底率 ----------
    n_reject = 0
    for it in oos:
        out, answer, cited, n_marker = run_one(it)
        rejected = bool(REJECT_PATTERN.search(answer))
        n_reject += int(rejected)
        rows.append({
            "id": it["id"], "has_answer": False, "question": it["question"],
            "rejected": rejected, "retrieve_count": out.get("retrieve_count"), "answer": answer,
        })
        print(f"[兜底] Q{it['id']:>2} {'正确拒答' if rejected else '★疑似硬答，需人工看'}（重检{out.get('retrieve_count')}轮）")

    reject_rate = n_reject / len(oos)
    snap = snapshot()

    lines = [
        "# 生成层评测结果（引用正确率 / 无答案兜底率）", "",
        f"- 时间：{snap['timestamp']}　commit：`{snap['git_commit']}`",
        f"- 知识库：{snap['collection']}（{snap['total_chunks']} 块）；生成温度 {snap['temp_answer']}，评估温度 {snap['temp_eval']}", "",
        "## 一、引用质量（%d 道有答案题）" % n_a, "",
        f"- 引用标注率（答案至少含 1 个 [n]）：**{n_with_marker}/{n_a} = {cite_marker_rate:.2%}**",
        f"- 引用精确率（题均 被引来源命中 golden 的比例）：**{cite_precision:.2%}**",
        f"- 正确引用覆盖率（至少引到 1 个正确文档）：**{n_cover}/{n_a} = {cite_cover_rate:.2%}**", "",
        "## 二、无答案题兜底（%d 道知识库外问题）" % len(oos), "",
        f"- 正确拒答率：**{n_reject}/{len(oos)} = {reject_rate:.2%}**", "",
        "> 拒答为关键词初判，逐题完整回答见同名 raw 明细，可人工复核。", "",
        "## 三、逐题明细", "",
        "| ID | 类型 | 结果 | 检索轮次 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        if r["has_answer"]:
            lines.append(f"| {r['id']} | 有答案 | 引用精确率 {r['cite_precision']:.2f}，{'命中' if r['cover'] else '未命中'} | {r['retrieve_count']} |")
        else:
            lines.append(f"| {r['id']} | 无答案 | {'正确拒答' if r['rejected'] else '★疑似硬答'} | {r['retrieve_count']} |")
    md = "\n".join(lines)

    summary = {
        "snapshot": snap,
        "citation": {"n": n_a, "marker_rate": round(cite_marker_rate, 4),
                     "precision": round(cite_precision, 4), "cover_rate": round(cite_cover_rate, 4)},
        "rejection": {"n": len(oos), "reject_rate": round(reject_rate, 4)},
    }

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"gen_raw_{stamp}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    (out_dir / f"gen_summary_{stamp}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"gen_summary_{stamp}.md").write_text(md, encoding="utf-8")

    print("\n" + "=" * 60)
    print(md)
    print("=" * 60)
    print(f"已写入 {out_dir}（前缀 gen_{stamp}）")


if __name__ == "__main__":
    main()
