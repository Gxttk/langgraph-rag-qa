"""RAG 检索消融评测。

四个配置（每次只加一个变量，保证控制变量公平）：
  A   裸检索        : 原问题直接 embedding → 单路向量检索（无改写、无多路、无精排）
  A'  仅改写        : 改写后只取第 1 个查询词做单路检索（A→A' 隔离出“改写本身”的收益）
  B   改写+多路召回 : 一次改写得到的全部查询词分别检索、去重合并，按向量分取前 4
                      （A'→B 隔离出“多路扩展”的收益）
  C   +Rerank 精排  : 与 B 完全相同的候选池，交给 LLM 重排后取前 4（B→C 隔离 Rerank 收益）

指标口径（文件级，chunk 候选先按 source 去重，每个文档只保留最高名次，再取 top-4）：
  Hit@4 : top-4 文档命中任一 golden_sources 记 1，全题平均
  MRR   : top-4 中第一个命中文档的名次倒数（1/0.5/0.333/0.25，未命中 0）

随机性控制：A 只用 embedding（确定），跑 1 次；A'/B/C 含查询改写（temperature=0.5），
重复 --repeat 次（默认 3），报告数据集级指标的均值与 [min,max] 波动。
C 的 Rerank 温度为 0 本身确定，但其候选池来自随机改写，故同样随 repeat 重算。

用法（在项目根目录）：
  python eval/run_eval.py                 # A/A'/B/C，重复 3 次
  python eval/run_eval.py --repeat 1      # 快速跑一遍
  python eval/run_eval.py --with-d        # 额外用前 10 题跑完整 graph（含重检闭环）
结果写入 eval/results/。
"""
import argparse
import json
import socket
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# 把项目根加入 sys.path，使脚本可直接 `python eval/run_eval.py` 运行
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.config import settings  # noqa: E402
from src.retriever import rerank, rewrite_query  # noqa: E402
from src.utils.llm import get_embeddings  # noqa: E402
from src.utils.vector import count_documents, get_client, search  # noqa: E402
from src.utils.logger import logger  # noqa: E402

# 屏蔽节点内部偏细的日志，让评测输出更干净
logger.remove()


# ---------- 指标工具 ----------
def dedup_by_source(hits):
    """chunk 级有序列表 → 文档级有序列表：同一 source 只保留最先出现（名次最高）的一条。"""
    seen = set()
    docs = []
    for h in hits:
        src = h["source"]
        if src not in seen:
            seen.add(src)
            docs.append({"source": src, "chunk_index": h.get("chunk_index"), "score": h.get("score")})
    return docs


def hit_mrr(doc_list, golden, k):
    """对（已按 source 去重、按名次排序的）文档列表计算 Hit@k 与 MRR。"""
    golden = set(golden)
    for rank, doc in enumerate(doc_list[:k], start=1):
        if doc["source"] in golden:
            return 1, 1.0 / rank
    return 0, 0.0


# ---------- 单次四配置检索 ----------
def run_configs_once(question, top_k):
    """对一道题跑一遍 A/A'/B/C，返回各配置的文档级 top-k 列表与诊断信息。"""
    # A：原问题单路裸检索（确定）
    a_vec = get_embeddings([question])[0]
    a_hits = search(a_vec, top_k=top_k)
    a_docs = dedup_by_source(a_hits)[:top_k]

    # A'/B/C 共用同一次改写结果，避免“不同次改写”引入额外变量
    queries = rewrite_query(question)
    q_vecs = get_embeddings(queries)

    # A'：只用第 1 个改写词单路
    ap_hits = search(q_vecs[0], top_k=top_k)
    ap_docs = dedup_by_source(ap_hits)[:top_k]

    # 召回池：全部改写词分别检索 → 按 text 去重 → 按向量分降序（B/C 共用此池）
    pool, seen_texts = [], set()
    for vec in q_vecs:
        for h in search(vec, top_k=top_k):
            if h["text"] not in seen_texts:
                seen_texts.add(h["text"])
                pool.append(h)
    pool.sort(key=lambda x: x["score"], reverse=True)

    # B：不精排，直接按向量分取（文件级去重后）top-k
    b_docs = dedup_by_source(pool)[:top_k]

    # C：同一候选池交给 LLM Rerank
    rerank_skipped = len(pool) <= top_k  # 与 retriever.rerank 内部跳过逻辑一致
    c_hits = rerank(question, pool, top_k=top_k)
    c_docs = dedup_by_source(c_hits)[:top_k]

    diag = {
        "n_queries": len(queries),
        "pool_chunk_size": len(pool),
        "pool_doc_size": len(dedup_by_source(pool)),
        "rerank_skipped": rerank_skipped,
    }
    return {"A": a_docs, "A_prime": ap_docs, "B": b_docs, "C": c_docs}, diag


def dataset_level_metrics(records, cfg, golden_map, top_k):
    """records: 本次 repeat 内 {id: docs}；返回该 repeat 的数据集级 Hit/MRR。"""
    hit_sum = rr_sum = 0
    n = 0
    for qid, docs in records.items():
        hit, rr = hit_mrr(docs, golden_map[qid], top_k)
        hit_sum += hit
        rr_sum += rr
        n += 1
    return hit_sum / n, rr_sum / n


# ---------- 知识库快照 ----------
def snapshot():
    info = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "collection": settings.collection_name,
        "metric": "COSINE",
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "retrieve_top_k": settings.retrieve_top_k,
        "rewrite_num_queries": settings.rewrite_num_queries,
        "temp_rewrite": settings.temp_rewrite,
        "temp_rerank": settings.temp_rerank,
        "total_chunks": count_documents(),
    }
    try:
        info["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception:
        info["git_commit"] = "unknown"
    # 每个来源文档各有多少 chunk
    try:
        client = get_client()
        rows = client.query(
            collection_name=settings.collection_name,
            filter="chunk_index >= 0",
            output_fields=["source"],
            limit=10000,
        )
        info["chunks_per_source"] = dict(sorted(Counter(r["source"] for r in rows).items()))
    except Exception as e:
        info["chunks_per_source"] = f"统计失败: {e}"
    return info


# ---------- 配置 D：完整 graph（含重检闭环），可选 ----------
def run_full_graph(questions, top_k, repeat):
    from langgraph.checkpoint.memory import InMemorySaver

    from src.graph import build_graph, build_initial_input

    per_repeat = []
    for r in range(repeat):
        graph = build_graph(InMemorySaver())
        records = {}
        for qid, q in questions:
            cfg = {"configurable": {"thread_id": f"eval-d-{r}-{qid}"}}
            out = graph.invoke(build_initial_input(q), config=cfg)
            records[qid] = dedup_by_source(out.get("hits", []))[:top_k]
        per_repeat.append(records)
    return per_repeat


def agg_repeats(repeat_records, golden_map, top_k):
    """repeat_records: [ {id: docs}, ... ] → 均值与波动。"""
    hits, rrs = [], []
    for records in repeat_records:
        h, m = dataset_level_metrics(records, None, golden_map, top_k)
        hits.append(h)
        rrs.append(m)
    return {
        "hit_mean": round(sum(hits) / len(hits), 4),
        "hit_min": round(min(hits), 4),
        "hit_max": round(max(hits), 4),
        "mrr_mean": round(sum(rrs) / len(rrs), 4),
        "mrr_min": round(min(rrs), 4),
        "mrr_max": round(max(rrs), 4),
        "runs": len(hits),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3, help="含改写配置的重复次数")
    parser.add_argument("--top-k", type=int, default=settings.retrieve_top_k)
    parser.add_argument("--with-d", action="store_true", help="额外跑完整 graph（配置 D）")
    parser.add_argument("--d-limit", type=int, default=10)
    args = parser.parse_args()

    # 评测前快速探测 Milvus 是否可达，避免向量库没起时卡在 gRPC 重试里
    try:
        with socket.create_connection((settings.milvus_host, int(settings.milvus_port)), timeout=3):
            pass
    except OSError:
        raise SystemExit(f"Milvus 不可达 {settings.milvus_host}:{settings.milvus_port}，请先 docker compose up -d standalone")

    # 读数据集
    items = [json.loads(line) for line in (ROOT / "eval" / "dataset.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    answerable = [it for it in items if it["has_answer"]]
    oos = [it for it in items if not it["has_answer"]]
    golden_map = {it["id"]: it["golden_sources"] for it in answerable}
    print(f"数据集：共 {len(items)} 题，其中有答案 {len(answerable)} 题参与检索指标，无答案 {len(oos)} 题（第二步测兜底）")

    snap = snapshot()
    print(f"快照：collection={snap['collection']} 总块数={snap['total_chunks']} commit={snap['git_commit']}")

    configs = ["A", "A_prime", "B", "C"]
    # A 确定只跑 1 次；A'/B/C 跑 repeat 次
    repeat_records = {c: [] for c in configs}
    raw, pool_sizes, doc_sizes, skip_count, skip_total = [], [], [], 0, 0

    for r in range(args.repeat):
        this = {c: {} for c in configs}
        for it in answerable:
            qid, q = it["id"], it["question"]
            res, diag = run_configs_once(q, args.top_k)
            for c in configs:
                # A 只在第 0 轮计算（确定性），避免重复 embedding 调用
                if c == "A" and r > 0:
                    continue
                this[c][qid] = res[c]
            if r == 0:
                pool_sizes.append(diag["pool_chunk_size"])
                doc_sizes.append(diag["pool_doc_size"])
                skip_total += 1
                if diag["rerank_skipped"]:
                    skip_count += 1
            raw.append({"repeat": r, "id": qid, "question": q, "golden": it["golden_sources"],
                        "diag": diag,
                        "top_docs": {c: [d["source"] for d in res[c]] for c in configs}})
            print(f"[repeat {r+1}/{args.repeat}] Q{qid:>2} 候选{diag['pool_chunk_size']}块/{diag['pool_doc_size']}文档"
                  f"{' [rerank跳过]' if diag['rerank_skipped'] else ''}")
        for c in configs:
            if c == "A" and r > 0:
                continue
            repeat_records[c].append(this[c])

    summary = {c: agg_repeats(repeat_records[c], golden_map, args.top_k) for c in configs}

    # 配置 D（可选）
    d_summary = None
    if args.with_d:
        dq = [(it["id"], it["question"]) for it in answerable][: args.d_limit]
        d_golden = {qid: golden_map[qid] for qid, _ in dq}
        d_records = run_full_graph(dq, args.top_k, args.repeat)
        d_summary = agg_repeats(d_records, d_golden, args.top_k)
        d_summary["n_questions"] = len(dq)

    diag_summary = {
        "pool_chunk_avg": round(sum(pool_sizes) / len(pool_sizes), 2),
        "pool_doc_avg": round(sum(doc_sizes) / len(doc_sizes), 2),
        "rerank_skip": f"{skip_count}/{skip_total} 题（候选<=top_k 时 C 退化为 B）",
    }

    result = {"snapshot": snap, "n_answerable": len(answerable), "n_out_of_scope": len(oos),
              "repeat": args.repeat, "top_k": args.top_k,
              "metrics": summary, "config_D_full_graph": d_summary, "diagnosis": diag_summary}

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"raw_{stamp}.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in raw), encoding="utf-8")
    (out_dir / f"summary_{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台 + Markdown 汇总
    lines = ["# 检索消融评测结果", "",
             f"- 时间：{snap['timestamp']}　commit：`{snap['git_commit']}`",
             f"- 知识库：{snap['collection']}（{snap['total_chunks']} 块，{snap['embedding_model']} dim={snap['embedding_dim']}，COSINE）",
             f"- 题量：{len(answerable)} 道有答案题，重复 {args.repeat} 次，top-{args.top_k}", "",
             "| 配置 | 含义 | Hit@4 均值 | Hit 波动 | MRR 均值 | MRR 波动 |",
             "|---|---|---|---|---|---|"]
    name_map = {"A": "裸检索（基线）", "A_prime": "仅改写（单路）", "B": "改写+多路召回", "C": "B+Rerank 精排"}
    for c in configs:
        m = summary[c]
        lines.append(f"| {c} | {name_map[c]} | {m['hit_mean']:.3f} | {m['hit_min']:.3f}~{m['hit_max']:.3f} "
                     f"| {m['mrr_mean']:.3f} | {m['mrr_min']:.3f}~{m['mrr_max']:.3f} |")
    if d_summary:
        lines.append(f"| D | 完整 graph（含重检，{d_summary['n_questions']}题） | {d_summary['hit_mean']:.3f} | "
                     f"{d_summary['hit_min']:.3f}~{d_summary['hit_max']:.3f} | {d_summary['mrr_mean']:.3f} | "
                     f"{d_summary['mrr_min']:.3f}~{d_summary['mrr_max']:.3f} |")
    lines += ["", f"- 候选池平均 {diag_summary['pool_chunk_avg']} 块 / {diag_summary['pool_doc_avg']} 文档",
              f"- Rerank 跳过：{diag_summary['rerank_skip']}", ""]
    md = "\n".join(lines)
    (out_dir / f"summary_{stamp}.md").write_text(md, encoding="utf-8")

    print("\n" + "=" * 60)
    print(md)
    print("=" * 60)
    print(f"明细与汇总已写入 {out_dir}（前缀 {stamp}）")


if __name__ == "__main__":
    main()
