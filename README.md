# langgraph-rag-qa

基于 **LangGraph + LangChain + Milvus** 的增强型检索增强生成（RAG）知识库问答系统。
在"向量检索 + 生成"基础链路上，实现了**查询改写、向量粗排 → LLM 精排的两阶段检索、检索质量评估闭环、引用溯源、多级异常降级**，并以 LangGraph 编排"检索—评估—重检"状态机。

## 一、核心特性

- **查询改写（Query Rewrite）**：将口语化、指代模糊的提问，由 LLM 扩写为多个专业检索词，多路独立召回后按文本去重合并，提升召回率。
- **两阶段检索**：第一阶段用向量相似度（COSINE）快速粗排召回，第二阶段由 LLM 对候选片段做相关性精排并截取 top-k，兼顾召回与精度。
- **检索质量评估闭环**：检索后由 LLM 判断召回片段是否足以回答问题；不足则带着评估反馈回到检索环节定向重检，达到重试上限自动放行，避免死循环。
- **引用溯源**：要求模型在句末标注来源编号，系统正则提取编号并回查检索片段，输出答案对应的文档名、段落号与相似度，事实可追溯、降低幻觉。
- **多级异常降级**：改写失败回退原问题、单路检索失败跳过、Embedding/向量库不可用与"知识库无匹配"两类空结果分别兜底，任一外部依赖故障都不会让流程崩溃。
- **多格式入库**：支持 PDF / Markdown / TXT，文档清洗 + 递归字符分块（按段落→换行→句号逐级切分，带 overlap 保持语义连贯），批量 Embedding 降低调用开销。

## 二、系统架构

```
                            用户问题
                               │
                    ┌──────────▼──────────┐
                    │   retrieve 检索节点  │
                    │ 查询改写 → 多路向量  │
                    │ 检索 → 按文本去重合并│
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   rerank 精排节点    │  LLM 对粗排结果重排，取 top-k
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  evaluate 评估节点   │  召回片段是否足以回答？
                    └──────────┬──────────┘
               不相关且未达上限 │   相关 / 已达上限上限
                    ┌──────────┴───────────┐
                    ▼                       ▼
             回到 retrieve            generate 生成节点
          （携带反馈定向重检）        生成答案 + 提取引用编号
                                            │
                                            ▼
                                 答案 + 引用来源（文档/段落/相似度）
```

## 三、技术栈

| 模块 | 选型 | 说明 |
|------|------|------|
| 编排框架 | LangGraph | 以状态机实现"检索—评估—重检"闭环 |
| LLM | DeepSeek-Chat | 查询改写、精排、质量评估、答案生成 |
| Embedding | DashScope text-embedding-v3 | 512 维，批量调用 |
| 向量数据库 | Milvus 2.6（standalone） | AUTOINDEX + COSINE，生产级向量库 |
| 文档处理 | PyPDF + RecursiveCharacterTextSplitter | PDF/MD/TXT 解析与递归分块 |
| 部署 | Docker Compose | 一键拉起 Milvus（etcd + minio + standalone） |

## 四、快速开始

### 1. 环境准备

```bash
# 建议使用 Python 3.10+ 虚拟环境
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，至少填入两个 Key：`DEEPSEEK_API_KEY`（大模型）与 `DASHSCOPE_API_KEY`（Embedding）。

### 3. 启动 Milvus 向量库

```bash
docker compose up -d
```

> 若本机 19530 端口已有 Milvus 在运行（多项目可共用一个实例，collection 相互隔离），可跳过本步直接复用。

### 4. 文档入库

把 PDF / Markdown / TXT 文档放入 `data/` 目录，然后执行：

```bash
python run.py --ingest
```

流程为：加载文档 → 文本清洗 → 递归分块 → 批量 Embedding → 写入 Milvus。

### 5. 提问

```bash
python run.py "LangGraph 的 State 是什么？"
```

输出包含：回答正文、引用来源（文档名 / 段落号 / 相似度）、检索轮次。

## 五、项目结构

```
langgraph-rag-qa/
├── data/                        # 知识库原始文档（md/txt/pdf）
├── src/
│   ├── ingest.py                # 加载→清洗→递归分块→批量向量化→入库
│   ├── retriever.py             # 查询改写 + 向量粗排 + LLM 精排 + 去重
│   ├── generator.py             # 基于上下文生成回答 + 引用溯源
│   ├── graph.py                 # LangGraph 编排（检索-评估-重检闭环）
│   └── utils/
│       ├── llm.py               # DeepSeek LLM 与 DashScope Embedding 封装
│       └── vector.py            # Milvus 连接、建表、写入、检索
├── run.py                       # CLI 入口（--ingest 入库 / 直接提问）
├── docker-compose.yml           # Milvus standalone 一键部署
├── requirements.txt             # 依赖与锁定版本
├── .env.example                 # 环境变量模板
└── README.md
```

## 六、关键设计

### 6.1 递归分块 + 重叠窗口

采用 `RecursiveCharacterTextSplitter`，按 `段落 → 换行 → 句号 → 空格 → 字符` 的优先级逐级切分（chunk_size=500，overlap=50），相比定长硬切更能保持句子与语义完整；重叠窗口避免关键信息被切断在两个块的边界。过短的无意义文本块会被过滤。

### 6.2 查询改写 + 两阶段检索

用户原始问题往往口语化、含指代，直接做向量检索召回率低。系统先让 LLM 把问题扩写为 3 个互补的专业查询词，分别做向量粗排召回并按**文本内容**去重（不同来源也可能转载相同文本，故不按 URL 去重）；再由 LLM 对合并后的候选做精排，输出编号顺序，截取相关性最高的 top-k 进入生成。

### 6.3 检索质量评估闭环

检索后取 top-3 片段交给 LLM 判断"是否足以回答问题"，输出 `is_relevant` 与 `feedback`。不相关时把 feedback 注入下一轮查询改写，做**定向**补检而非盲目重试；重试次数由 `MAX_RETRIEVE_RETRIES` 限制，达到上限强制放行，保证流程一定能收敛。

### 6.4 引用溯源

生成 Prompt 要求模型在引用处标注 `[1] [2]` 编号；系统用正则提取这些编号，回查对应检索片段，最终输出答案真正引用到的文档名、段落号与相似度，形成可核对的引用链。

### 6.5 健壮性与降级策略

| 故障点 | 降级策略 |
|--------|----------|
| 查询改写 LLM 调用失败 | 回退为直接使用原始问题检索 |
| 单个查询向量检索失败 | 跳过该路，其余查询继续 |
| Embedding 服务失败 / 全部查询失败 | 标记"检索服务不可用"，跳过精排与无谓重检，直接给出明确提示 |
| LLM 精排失败 | 回退为向量粗排的原始顺序 |
| 质量评估 LLM 失败 / 达到重试上限 | 默认放行进入生成，避免流程卡死 |
| 检索成功但无匹配 | 与"服务不可用"区分，提示知识库中暂无相关内容 |

## 七、配置项说明（.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | - | DeepSeek 大模型密钥（必填） |
| `DEEPSEEK_MODEL` | deepseek-chat | 生成 / 改写 / 评估所用模型 |
| `DASHSCOPE_API_KEY` | - | 阿里云 DashScope Embedding 密钥（必填） |
| `MILVUS_HOST` / `MILVUS_PORT` | localhost / 19530 | Milvus 连接地址 |
| `MILVUS_COLLECTION` | langgraph_rag_kb | 向量集合名 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 500 / 50 | 分块长度与重叠 |
| `RETRIEVE_TOP_K` | 4 | 每个查询召回条数（精排后保留条数） |
| `REWRITE_NUM_QUERIES` | 3 | 查询改写生成的检索词数量 |
| `MAX_RETRIEVE_RETRIES` | 2 | 质量不达标时的最大重检次数 |
