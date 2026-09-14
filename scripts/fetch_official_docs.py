# -*- coding: utf-8 -*-
"""
批量抓取 LangChain/LangGraph 官方文档为 Markdown，落到 data/ 作为 RAG 语料。
- 通过 r.jina.ai 读者模式把官方文档页转成干净 markdown（MIT 开源文档，可自由使用）
- 可重复运行：已存在且非空的文件默认跳过，加 --force 重新抓取
运行：python scripts/fetch_official_docs.py [--force]
"""
import re
import sys
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# (输出文件名, 官方文档 URL)
DOCS = [
    ("01_lgraph_state_lowlevel.md", "https://langchain-ai.github.io/langgraph/concepts/low_level/"),
    ("02_lgraph_persistence.md", "https://langchain-ai.github.io/langgraph/concepts/persistence/"),
    ("03_lgraph_human_in_loop.md", "https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/"),
    ("04_lgraph_streaming.md", "https://langchain-ai.github.io/langgraph/concepts/streaming/"),
    ("05_lgraph_multi_agent.md", "https://langchain-ai.github.io/langgraph/concepts/multi_agent/"),
    ("06_lc_text_splitters.md", "https://python.langchain.com/docs/concepts/text_splitters/"),
    ("07_lc_embeddings.md", "https://python.langchain.com/docs/concepts/embedding_models/"),
    ("08_lc_vectorstores.md", "https://python.langchain.com/docs/concepts/vectorstores/"),
    ("09_lc_retrievers.md", "https://python.langchain.com/docs/concepts/retrievers/"),
    ("10_lc_tools.md", "https://python.langchain.com/docs/concepts/tools/"),
    ("11_lc_rag_tutorial.md", "https://python.langchain.com/docs/tutorials/rag/"),
]

BAD_MARKERS = ("page not found", "404 not found", "could not find",
               "access denied", "rate limit", "something went wrong")
HEADERS = {"User-Agent": "Mozilla/5.0", "X-Return-Format": "markdown"}


def clean_jina(text: str) -> str:
    """去掉 jina reader 加的 Title/URL Source/Markdown Content 头部。"""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(("Title: ", "URL Source: ", "Markdown Content:")):
            continue
        out.append(ln)
    body = "\n".join(out)
    body = re.sub(r"^\s*-{3,}\s*\n", "", body, count=1)  # 去首个分隔线
    return body.strip()


def fetch_one(url: str, retries: int = 3):
    jina_url = "https://r.jina.ai/" + url
    last_err = ""
    for attempt in range(retries):
        try:
            resp = requests.get(jina_url, headers=HEADERS, timeout=90)
            body = clean_jina(resp.text)
            head = body[:2000].lower()
            if len(body) >= 800 and not any(m in head for m in BAD_MARKERS):
                return body, ""
            last_err = f"内容过短或错误页（{len(body)} 字符，HTTP {resp.status_code}）"
        except Exception as e:  # noqa
            last_err = repr(e)
        time.sleep(3)
    return None, last_err


def main():
    force = "--force" in sys.argv
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ok, fail, skip = [], [], []
    for name, url in DOCS:
        dst = DATA_DIR / name
        if dst.exists() and dst.stat().st_size > 800 and not force:
            skip.append(name)
            print(f"[SKIP] {name}（已存在）")
            continue
        print(f"[GET ] {name} <- {url}", flush=True)
        body, err = fetch_one(url)
        if body:
            dst.write_text(body + "\n", encoding="utf-8")
            ok.append(name)
            print(f"       OK，{len(body)} 字符")
        else:
            fail.append((name, url, err))
            print(f"       FAIL：{err}")
        time.sleep(2)  # 礼貌限速
    print("\n===== 汇总 =====")
    print(f"成功 {len(ok)} / 跳过 {len(skip)} / 失败 {len(fail)}")
    for name, url, err in fail:
        print(f"  失败：{name}  {url}  -> {err}（可稍后重跑或用浏览器加 r.jina.ai 前缀手动存）")


if __name__ == "__main__":
    main()
