# 增强型 RAG 知识库问答系统

基于 LangChain + LangGraph + Milvus 实现的检索增强生成（RAG）系统，支持查询改写、来源溯源、检索质量评估循环。

## 一、项目特性

- **查询改写（Query Rewrite）**：LLM 将口语化提问扩写为多个专业查询词，批量检索后合并去重，提升召回准确率
- **来源溯源**：回答中标注引用来源编号，末尾列出引用的文档名、段落、相似度，降低幻觉提升可信度
- **检索质量评估循环**：LangGraph 编排，检索后 LLM 评估召回相关性，不达标则自动重检，最多重试2次
- **多格式文档支持**：PDF / Markdown / TXT，递归字符分块（chunk_size=500, overlap=50）
- **Milvus 向量库**：Docker standalone 部署，DashScope text-embedding-v3（dim=512），COSINE 相似度检索

## 二、技术架构

```
用户问题
  ↓
[查询改写] LLM 扩写为 3 个专业查询词
  ↓
[批量检索] 每个查询词 Milvus 检索 top-4，去重合并
  ↓
[质量评估] LLM 判断召回片段是否相关
  ├─ 不相关且未达上限 → 回到 [查询改写] 重检
  └─ 相关或达上限 → [生成回答]
  ↓
[生成回答] 基于检索结果生成，标注来源编号
  ↓
回答 + 引用来源
```

## 三、技术栈

| 模块 | 选型 | 说明 |
|------|------|------|
| 编排框架 | LangGraph | 检索-评估-重检循环 |
| LLM | DeepSeek | 回答生成 + 查询改写 + 质量评估 |
| Embedding | DashScope text-embedding-v3 | dim=512 |
| 向量库 | Milvus standalone | Docker 部署，AUTOINDEX + COSINE |
| 文档解析 | PyPDF / RecursiveCharacterTextSplitter | PDF/MD/TXT 分块 |

## 四、快速开始

### 1. 环境准备

```bash
# 复制环境变量模板
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY
```

确保 Milvus 已启动（Docker standalone）：
```bash
docker compose up -d  # 需要 etcd + minio + milvus-standalone
```

### 2. 文档入库

将 PDF/MD/TXT 文件放入 `data/` 目录，然后：

```bash
python run.py --ingest
```

### 3. 提问

```bash
python run.py "你的问题"
```

## 五、项目结构

```
rag_demo/
├── src/
│   ├── ingest.py          # 文档加载、分块、向量化、入库
│   ├── retriever.py       # 查询改写 + 向量检索 + 去重
│   ├── generator.py       # 回答生成 + 来源溯源
│   ├── graph.py           # LangGraph 编排（检索-评估-重检循环）
│   └── utils/
│       ├── llm.py         # DeepSeek LLM + DashScope Embedding
│       └── vector.py      # Milvus 连接与检索
├── data/                  # 待入库文档
├── run.py                 # 命令行入口
├── .env.example
└── README.md
```

## 六、核心设计

### 查询改写

用户原始问题可能口语化、表述模糊，直接检索召回率低。系统先调用 LLM 将问题扩写为 2~3 个专业查询词，每个查询词独立检索后合并去重。

**示例**：
- 原始问题："这东西咋用？"
- 改写后：["XXX 使用方法", "XXX 操作指南", "XXX 功能说明"]

### 来源溯源

生成回答时要求 LLM 在引用处标注 `[1]`、`[2]` 等来源编号，系统提取编号后匹配对应的检索结果，输出引用的文档名、段落号、相似度。用户可追溯回答的事实依据。

### 检索质量评估循环

检索完成后，LLM 评估 top-3 片段是否能回答用户问题。如果不相关，自动回到查询改写环节重新检索（最多2次）。避免因检索质量差导致回答"资料中未提及"。

## 七、面试讲点

1. **查询改写的价值**：解决用户口语化提问导致的召回不准问题，用 LLM 做 query expansion 是 RAG 优化的标准手段
2. **来源溯源的工程实现**：LLM 输出中标注编号，正则提取后匹配检索结果，实现可追溯的引用链
3. **检索质量评估循环**：用 LangGraph 实现"检索-评估-重检"闭环，和主力项目的评估-优化器模式方法论一致
4. **分块策略选择**：递归字符分块（按段落→换行→句号→空格→字符逐级切分），overlap=50 保证上下文连贯
5. **向量库选型**：Milvus 生产级 vs Chroma demo 级，AUTOINDEX 自适应索引，COSINE 相似度适合文本语义检索
