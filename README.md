# langgraph-rag-qa

基于 **LangGraph + LangChain + Milvus + FastAPI** 的**多轮对话式 RAG 知识库问答服务**。
在"向量检索 + 生成"的基础链路上，实现了**会话记忆与指代消解、查询改写、向量粗排 → LLM 精排两阶段检索、检索质量评估闭环、引用溯源、分节点温度控制、多级异常降级**，并以 LangGraph 编排状态机，对外提供 **SSE 流式**与普通 JSON 两种 HTTP 接口，支持 Docker 一键部署。

## 一、核心特性

- **多轮会话记忆与指代消解**：用 `add_messages` 累积对话历史、按 `session_id` 隔离不同会话；每轮先由 `condense` 节点结合历史把"那它呢 / 上面那个"等残句补全为独立问题（首轮无历史时免一次 LLM 调用）。检索用补全后的完整问题保证召回，生成用用户原话 + 历史保证回答自然。
- **查询改写（Query Rewrite）**：将口语化提问由 LLM 扩写为多个互补的专业检索词，多路独立召回后按**文本内容**去重合并，提升召回率。
- **两阶段检索**：第一阶段向量相似度（COSINE）粗排召回，第二阶段 LLM 对候选片段相关性精排并截取 top-k，兼顾召回与精度。
- **检索质量评估闭环**：检索后由 LLM 判断召回片段是否足以回答；不足则带着反馈定向重检，达到重试上限自动放行，保证流程收敛、不卡死。
- **引用溯源**：要求模型句末标注来源编号，系统正则提取并回查片段，输出文档名、段落号、相似度，事实可核对。
- **SSE 流式输出**：`astream_events` 单流同时推送节点进度与最终答案的逐字 token，通过 `metadata.langgraph_node` 只外显生成节点的 token，其余内部 LLM 调用不打扰用户。
- **分节点温度控制**：精排 0.0、评估/补全 0.1、生成 0.2 求确定防幻觉，查询改写 0.5 增加表达多样性，按节点注入而非全局一刀切。
- **多级异常降级**：改写失败回退原问题、单路检索失败跳过、Embedding/向量库不可用与"知识库无匹配"两类空结果分别兜底，任一外部依赖故障都不会让服务崩溃。
- **工程化与部署**：集中式配置（`config.py`）、loguru 结构化日志、Pydantic 参数校验、CORS、全局异常处理，Dockerfile + docker-compose 一键起整套服务。

## 二、系统架构

```
                 用户消息（以 session_id 标识会话）
                          │
              messages 历史（add_messages 跨轮累积）
                          │
                ┌─────────▼──────────┐
                │ condense 问题补全   │ 结合历史消解指代 → standalone 问题（首轮免 LLM）
                └─────────┬──────────┘
                          │ 用补全后的独立问题检索，保证召回
                ┌─────────▼──────────┐
                │ retrieve 检索节点   │ 查询改写 → 多路向量检索 → 按文本去重合并
                └─────────┬──────────┘
                          │
                ┌─────────▼──────────┐
                │ rerank 精排节点     │ LLM 对粗排候选重排，取 top-k
                └─────────┬──────────┘
                          │
                ┌─────────▼──────────┐
                │ evaluate 评估节点   │ 召回片段是否足以回答？
                └─────────┬──────────┘
        不相关且未达上限   │   相关 / 已达上限
               ┌──────────┴───────────┐
               ▼                       ▼
       回到 retrieve            generate 生成节点
   （携带反馈定向重检）        用原话 + 历史生成、提取引用编号，并把本轮问答写回 messages
                                       │
                                       ▼
                         答案 + 引用来源（文档 / 段落 / 相似度）
```

## 三、技术栈

| 模块 | 选型 | 说明 |
|------|------|------|
| 编排框架 | LangGraph | 状态机实现"补全—检索—精排—评估—重检—生成"闭环，checkpointer 维护会话 |
| LLM | DeepSeek-Chat | 问题补全、查询改写、精排、评估、答案生成 |
| Embedding | DashScope text-embedding-v3 | 512 维，批量调用 |
| 向量数据库 | Milvus 2.6（standalone） | AUTOINDEX + COSINE |
| 文档处理 | PyPDF + RecursiveCharacterTextSplitter | PDF/MD/TXT 解析与递归分块 |
| Web 服务 | FastAPI + Uvicorn + Pydantic | REST/SSE 接口、参数校验、自动 OpenAPI 文档 |
| 会话持久化 | InMemorySaver（可替换 PostgresSaver） | 按 thread_id 隔离多轮会话 |
| 日志 / 配置 | loguru / python-dotenv | 统一日志与集中配置 |
| 部署 | Docker + Docker Compose | 一键拉起 Milvus 与应用 |

## 四、快速开始（本地）

```bash
# 1. 安装依赖（建议 Python 3.10+ 虚拟环境）
pip install -r requirements.txt

# 2. 配置环境变量，至少填入 DEEPSEEK_API_KEY 与 DASHSCOPE_API_KEY
cp .env.example .env

# 3. 启动 Milvus（若本机 19530 已有 Milvus 在跑，可跳过、直接复用）
docker compose up -d standalone

# 4. 把 data/ 目录下的 PDF/MD/TXT 文档入库
python run.py --ingest
```

三种使用方式：

```bash
# ① 单轮问答（命令行，问完即止）
python run.py "LangGraph 的 State 是什么？"

# ② 多轮客服（命令行，可连续追问、理解指代；输入「新会话」清空、「退出」结束）
python chat.py

# ③ 启动 HTTP 服务，浏览器打开 http://localhost:8000/docs 交互式调试
python api.py
```

## 五、HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查，返回服务状态、Milvus 是否可连、库内文档块数 |
| POST | `/chat` | **SSE 流式**多轮问答，逐 token 返回 |
| POST | `/chat/sync` | 非流式多轮问答，整段 JSON 一次性返回 |
| GET | `/session/{session_id}/history` | 查询指定会话的完整对话历史 |

请求体：`{"message": "用户问题", "session_id": "可选，不传则新建；同一 ID 连续传入即多轮"}`

非流式调用示例：

```bash
curl -X POST http://localhost:8000/chat/sync \
  -H "Content-Type: application/json" \
  -d '{"message": "LangGraph 的 State 是什么", "session_id": "s1"}'

# 紧接着用同一个 session_id 追问残句，服务端会自动补全为独立问题
curl -X POST http://localhost:8000/chat/sync \
  -H "Content-Type: application/json" \
  -d '{"message": "那它怎么更新呢", "session_id": "s1"}'
```

流式调用（SSE）依次推送以下事件：`session`（会话 ID）→ `status`（节点进度）→ 多个 `token`（答案逐字）→ `sources`（引用来源）→ `metadata`（补全问题、检索轮次）→ `done`；异常时推送 `error`。

```bash
curl -N -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "什么是检查点"}'
```

## 六、Docker 部署

**方式 A：全新环境，一条命令拉起整套（Milvus + 应用）**

```bash
cp .env.example .env          # 填好你自己的 Key
docker compose up -d standalone                         # 1. 起向量库并等待 healthy
docker compose run --rm app python run.py --ingest      # 2. 文档一次性入库
docker compose up -d app                                # 3. 起问答服务
# 访问 http://localhost:8000/docs
```

**方式 B：只构建应用镜像，连接本机已在运行的 Milvus**

```bash
docker build -t langgraph-rag-qa .
docker run -d --name rag-api -p 8000:8000 \
  --env-file .env \
  -e MILVUS_HOST=host.docker.internal \
  langgraph-rag-qa
```

> 容器网络要点：容器内 `localhost` 指容器自身，连不到别的容器或宿主机服务。compose 内部用**服务名** `standalone` 连 Milvus；容器连**宿主机**上的 Milvus 用 `host.docker.internal`。密钥通过 `--env-file` / `-e` 运行时注入，绝不打进镜像。

## 七、项目结构

```
langgraph-rag-qa/
├── data/                        # 知识库原始文档（md/txt/pdf）
├── eval/                        # 评测：标注集 dataset、消融脚本、results 结果
├── src/
│   ├── config.py                # 集中配置：所有环境变量与超参唯一来源
│   ├── ingest.py                # 加载→清洗→递归分块→批量向量化→入库
│   ├── retriever.py             # 查询改写 + 向量粗排 + LLM 精排 + 去重
│   ├── generator.py             # 结合历史生成回答 + 引用溯源 + 历史格式化
│   ├── graph.py                 # LangGraph 编排（多轮 State + 五节点状态机）
│   └── utils/
│       ├── llm.py               # DeepSeek LLM（按温度缓存）与 DashScope Embedding
│       ├── vector.py            # Milvus 连接、建表、写入、检索、计数
│       └── logger.py            # loguru 统一日志
├── run.py                       # 单轮 CLI（--ingest 入库 / 直接提问）
├── chat.py                      # 多轮客服 CLI（InMemorySaver 维持会话）
├── api.py                       # FastAPI 服务（SSE / JSON / health / history）
├── Dockerfile                   # 应用镜像
├── docker-compose.yml           # etcd + minio + milvus + app 一键编排
├── requirements.txt             # 依赖与锁定版本
├── .env.example                 # 环境变量模板
└── README.md
```

## 八、关键设计

### 8.1 多轮记忆如何实现

- `State.messages` 使用 LangGraph 的 `add_messages` reducer，配合 checkpointer 按 `thread_id`（即 `session_id`）跨多轮累积；不同会话天然隔离。
- 每轮调用通过 `build_initial_input` 把 `hits / retrieve_count / feedback` 等**单轮工作字段重置**，只保留 `messages` 累积，避免上一轮的中间结果污染下一轮。
- `condense` 节点负责指代消解：取本轮之前的历史 + 当前问题，补全成脱离上下文也能理解的独立问题；首轮无历史直接透传、不调用 LLM。
- **检索 / 精排 / 评估用补全后的独立问题**（语义完整、召回准），**生成用用户原始问题 + 历史**（保留原话、回答自然并能承接）。
- 开发期用进程内 `InMemorySaver`；生产环境换成 `PostgresSaver` 只需改 `build_graph(checkpointer=...)` 一处，业务节点零改动。

### 8.2 递归分块 + 重叠窗口

`RecursiveCharacterTextSplitter` 按 `段落 → 换行 → 句号 → 空格 → 字符` 的优先级逐级切分（chunk_size=500，overlap=50），相比定长硬切更能保持语义完整；重叠窗口避免关键信息被切断在两块边界，过短的无意义块会被过滤。

### 8.3 查询改写 + 两阶段检索

原始问题往往口语化、含指代，直接向量检索召回率低。系统先扩写为 3 个互补检索词分别粗排召回，按**文本内容**去重（不同 URL 也可能转载相同文本，故不按 URL 去重）；再由 LLM 对合并候选精排输出编号顺序，截取 top-k 进入生成。

### 8.4 检索质量评估闭环

检索后取 top-3 片段交 LLM 判断是否足以回答，输出 `is_relevant` 与 `feedback`；不相关时把反馈注入下一轮改写做**定向**补检而非盲目重试，重试次数由 `MAX_RETRIEVE_RETRIES` 限制，达上限强制放行保证收敛。

### 8.5 引用溯源

生成 Prompt 要求模型在引用处标注 `[1] [2]`；系统正则提取编号回查片段，输出答案真正引用到的文档名、段落号、相似度，形成可核对的引用链；资料缺失时要求模型明确回答"资料中未提及"。

### 8.6 分节点温度

| 节点 | 温度 | 理由 |
|------|------|------|
| rerank 精排 | 0.0 | 排序要求唯一、可复现 |
| evaluate 评估 / condense 补全 | 0.1 | 结构化判断，尽量确定 |
| generate 生成 | 0.2 | 表达自然但不发散、抑制幻觉 |
| rewrite 查询改写 | 0.5 | 需要多样的检索表达以提高召回 |

### 8.7 健壮性与降级策略

| 故障点 | 降级策略 |
|--------|----------|
| 问题补全 LLM 失败 | 回退为直接使用原始问题 |
| 查询改写 LLM 失败 | 回退为直接使用原始问题检索 |
| 单个查询向量检索失败 | 跳过该路，其余查询继续 |
| Embedding 失败 / 全部查询失败 | 标记"检索服务不可用"，跳过精排与无谓重检，直接给出明确提示 |
| LLM 精排失败 | 回退为向量粗排的原始顺序 |
| 质量评估失败 / 达到重试上限 | 默认放行进入生成，避免流程卡死 |
| 检索成功但无匹配 | 与"服务不可用"区分，提示知识库暂无相关内容 |
| 生成 LLM 失败 | 返回友好错误而非抛出堆栈（HTTP 层由全局异常处理兜底） |

## 九、效果评测（消融实验）

`eval/` 下自建 30 题标注集：25 道检索题覆盖全部文档、5 道知识库外拒答题，问题均为口语化表述。检索层做控制变量消融，含 LLM 的配置重复 3 次取均值；候选先按来源文档去重再取 top-4，脚本、逐题明细与汇总均在 `eval/results/`。

### 9.1 检索层：Hit@4 / MRR

| 配置 | 做法 | Hit@4（均值/波动） | MRR（均值/波动） |
|------|------|------|------|
| A 裸检索 | 原问题直接单路向量检索 | 0.880（确定） | 0.800（确定） |
| A′ 仅改写 | 只用第 1 个改写词单路检索 | 0.907（0.84~0.96） | 0.753（0.70~0.83） |
| B 改写+多路召回 | 3 个改写词分别召回、去重合并 | **1.000（稳定）** | 0.809（0.79~0.83） |
| C B + LLM Rerank | 与 B 同一候选池做精排 | **1.000（稳定）** | **0.842（0.83~0.85）** |

结论：

- **多路召回是召回率的主要来源**：A→B 的 Hit@4 从 0.88 稳定提升到 1.00；
- 只改写、不多路（A′）收益不稳、波动大，说明多路冗余比单次改写更可靠；
- 召回饱和后，Rerank 不再改变"能否找到"，而是把正确文档排得更靠前（MRR 0.809→0.842），这正是精排的职责。

### 9.2 生成层：引用质量与拒答兜底（完整链路）

- **引用标注率 100%**（25/25 答案均带来源编号）、**正确引用覆盖率 100%**（每题至少引到标准来源文档），验证溯源链路有效；
- 引用精确率 65%：模型存在"过度引用"倾向（倾向标注 top-k 中所有沾边的块，部分为同主题旁证文档而非该题的标准来源），可通过"只标注直接支撑结论的来源"类 prompt 进一步收敛；
- 知识库外 **5 题正确拒答率 100%**：均在重检达到上限后回答"资料中未提及"，不拿不相关片段硬答。

复现命令：

```bash
python eval/run_eval.py --repeat 3      # 检索层消融（Hit@4 / MRR）
python eval/run_generation_eval.py      # 生成层引用与拒答评测
```

## 十、配置项说明（.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | - | DeepSeek 密钥（必填） |
| `DEEPSEEK_MODEL` | deepseek-chat | 各节点所用模型 |
| `DASHSCOPE_API_KEY` | - | DashScope Embedding 密钥（必填） |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | text-embedding-v3 / 512 | 嵌入模型与向量维度（须与建表一致） |
| `MILVUS_HOST` / `MILVUS_PORT` | localhost / 19530 | Milvus 地址 |
| `MILVUS_COLLECTION` | langgraph_rag_kb | 向量集合名 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 500 / 50 | 分块长度与重叠 |
| `EMBED_BATCH_SIZE` / `MIN_CHUNK_LEN` | 10 / 20 | 批量嵌入大小 / 过滤短块阈值 |
| `RETRIEVE_TOP_K` | 4 | 每路召回数（精排后保留数） |
| `REWRITE_NUM_QUERIES` | 3 | 查询改写生成的检索词数量 |
| `MAX_RETRIEVE_RETRIES` | 2 | 质量不达标时的最大重检次数 |
| `MAX_HISTORY_MESSAGES` | 6 | 补全 / 生成携带的最近消息条数 |
| `TEMP_CONDENSE/RERANK/EVAL/ANSWER/REWRITE` | 0.1/0.0/0.1/0.2/0.5 | 分节点温度 |
| `API_HOST` / `API_PORT` | 0.0.0.0 / 8000 | 服务监听地址与端口 |
| `LOG_LEVEL` | INFO | 日志级别 |
