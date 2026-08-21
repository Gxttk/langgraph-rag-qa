"""文档入库：加载 → 清洗 → 分块 → 向量化 → Milvus 存储。

支持格式：.md / .txt / .pdf
分块策略：递归字符分块，chunk_size=500, overlap=50
"""
import os
import re
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from .utils.llm import get_embeddings
from .utils.vector import clear_collection, insert_chunks

load_dotenv(encoding="utf-8")

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))
BATCH_SIZE = 10  # embedding 批量调用大小
MIN_CHUNK_LEN = 20  # 过滤过短的无意义文本块


def _load_pdf(path: Path) -> str:
    """读取 PDF 全文。"""
    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        pages.append(f"[第{i + 1}页]\n{text}")
    return "\n".join(pages)


def _load_text(path: Path) -> str:
    """读取纯文本 / Markdown。"""
    return path.read_text(encoding="utf-8", errors="ignore")


def load_document(path: Path) -> str:
    """根据扩展名加载文档内容。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _load_pdf(path)
    if suffix in (".md", ".txt"):
        return _load_text(path)
    raise ValueError(f"不支持的文件格式: {suffix}")


def clean_text(text: str) -> str:
    """文档清洗：去除乱码、多余空白、统一格式。

    处理内容：
    - 去除控制字符和 PDF 提取乱码
    - 统一换行符（\\r\\n / \\r → \\n）
    - 去除行尾空格
    - 压缩连续空行（3个以上→2个）
    - 压缩连续空格（2个以上→1个）
    """
    # 去除控制字符（保留换行和制表符）
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # 统一换行符
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 去除行尾空格
    text = re.sub(r"[ \t]+\n", "\n", text)
    # 压缩连续空行（3个以上换行→2个）
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 压缩连续空格（2个以上→1个）
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def split_text(text: str, source: str) -> List[dict]:
    """递归字符分块，返回带元数据的文本块列表，过滤过短的无意义块。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", "。", "！", "？", ".", " ", ""],
    )
    chunks = splitter.split_text(text)
    # 过滤过短的无意义文本块
    valid_chunks = [c for c in chunks if len(c.strip()) >= MIN_CHUNK_LEN]
    return [
        {"text": chunk, "source": source, "chunk_index": i}
        for i, chunk in enumerate(valid_chunks)
    ]


def embed_and_store(chunks: List[dict]):
    """批量向量化并写入 Milvus。"""
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["text"] for c in batch]
        vectors = get_embeddings(texts)
        records = [
            {**c, "vector": v}
            for c, v in zip(batch, vectors)
        ]
        insert_chunks(records)


def ingest_directory(data_dir: str):
    """扫描目录下所有文档，逐个加载、分块、入库。"""
    data_path = Path(data_dir)
    if not data_path.exists():
        print(f"[ingest] 目录不存在: {data_dir}")
        return

    supported = {".md", ".txt", ".pdf"}
    files = [f for f in data_path.iterdir() if f.suffix.lower() in supported]

    if not files:
        print(f"[ingest] {data_dir} 下没有支持的文档（.md/.txt/.pdf）")
        return

    # 入库前清空旧数据，避免重复入库
    clear_collection()

    total_chunks = 0
    for f in files:
        print(f"[ingest] 处理: {f.name}")
        raw_text = load_document(f)
        text = clean_text(raw_text)
        chunks = split_text(text, source=f.name)
        embed_and_store(chunks)
        total_chunks += len(chunks)

    print(f"[ingest] 完成，共处理 {len(files)} 个文档，{total_chunks} 个文本块")


if __name__ == "__main__":
    import sys
    directory = sys.argv[1] if len(sys.argv) > 1 else "data"
    ingest_directory(directory)
