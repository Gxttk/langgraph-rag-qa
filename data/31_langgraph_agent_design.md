# LangGraph 调研 Agent：双子图架构与工程设计

## 0. 技术栈速览
Python、LangChain / LangGraph（工作流编排）、DeepSeek（LLM）、Tavily（网页搜索）、Milvus + DashScope embedding（本地向量检索）、PostgreSQL（LangGraph Checkpoint 持久化）、FastAPI + uvicorn（SSE 接口）、Docker / Docker Compose（部署）、loguru（日志）。

## 1. 三档项目介绍

### 一句话
基于 LangGraph 的深度调研 Agent，把官方扁平单循环重构成主图加双子图，带双层循环、缺数据逃逸、人在回路和 PostgreSQL 持久化，最后用 FastAPI + Docker 服务化部署。

### 30 秒
官方 Research Agent 模板是 plan→search→scrape→write→critique 的扁平单循环，只有单路网页检索、只补资料不优化写作、无状态隔离、无持久化和人工介入。我重构成主图加检索、打磨两个子图：检索子图做网页+本地向量库双路并行检索并写初稿，主图 critique 当门卫判断信息够不够，打磨子图做写作质量评审与重写，并设计了"缺数据就逃逸回检索"的路径，最后加人在回路审核。工程上用 PostgreSQL 做 checkpoint 支撑中断恢复，astream_events 做流式，FastAPI 暴露 SSE 接口，Docker Compose 一键编排部署。

### 详细说明
> 这是一个基于 LangGraph 的深度调研 Agent，输入主题，自动检索、写报告、自我评审、多轮修改，最后人工审核输出。我是在官方 Research Agent 模板上做的深度改造。原生模板是 5 个节点的扁平单循环：规划、网页搜索、抓正文、写报告、信息评审，不通过就整体重跑，问题是只有单路网页检索、只有一层补资料循环却不优化写作质量、节点全扁平没有状态隔离，也没有持久化和人工介入。
>
> 我把它重构成"主图+两个子图"。检索子图负责规划后网页和本地向量库双路并行检索、合并去重、抓正文、写初稿；打磨子图负责质量评审和重写。主图上 critique 当门卫判断信息够不够，不够回检索，形成最多三轮的外层循环；质量评审判断写得好不好，写得差就在打磨子图内重写最多两次，但如果根因是缺数据，就通过逃逸机制跳回检索子图补素材，而不是空转重写。最后人工审核节点用 interrupt 暂停，人可通过或提意见，靠 PostgreSQL 的 checkpoint 用 Command(resume) 恢复。
>
> 工程上用 astream_events 一边出节点进度一边把写报告 token 做打字机效果，FastAPI 包成 SSE 接口，最后 Docker Compose 把应用、PostgreSQL、Milvus 编排成一套、一条命令起。

## 2. 数据流主线（理解了就不会乱）
```
用户主题
 →【检索子图】plan 拆搜索词 → [web(Tavily) ‖ rag(Milvus)] 双路并行
 → merge 合并去重(loop_count+1) → scrape 抓正文(失败回退摘要) → write_report 初稿
 →【主图 critique 门卫】信息够不够？
       不够→回检索子图（外层循环 loop_count<3）；够→↓
 →【打磨子图】quality_review 写得好不好？
       缺数据(needs_more_info)→逃逸回检索；写得差→重写(rewrite_count<2，写完回主图再过critique)；好→↓
 →【human_review 人在回路】interrupt 等人审
       提意见→Command(resume)回打磨重写；通过→输出
 全程：PostgreSQL 存 checkpoint；astream_events 流式；FastAPI SSE；Docker 部署
```
三个循环别混：**外层=信息不够回检索；内层=写得差重写；逃逸=缺数据回检索（内层把控制权交回外层）**。

## 3. 原生缺陷 → 我的改造（对照表，"为什么做"用）
| 原生模板缺陷 | 我的改造 |
|---|---|
| 只有单路网页检索 | 加本地 Milvus RAG，双路 fan-out/fan-in，互为冗余 |
| critique 只评信息完整性、不评写作质量 | 增加 quality_review 专评写作质量，形成双层闭环 |
| 不通过只能整体回 plan 重跑 | 区分"缺信息/写得差/缺数据"，分别路由到检索、重写、逃逸 |
| 全节点扁平、状态互相污染 | 主图+双子图，状态隔离，主图只留 9 个跨阶段字段 |
| 无持久化，进程结束状态丢失 | PostgresSaver 存 checkpoint，三元主键，可跨请求恢复 |
| 无人工介入 | human_review + interrupt + Command(resume) 人在回路 |
| 同步一次性返回、体验差 | astream_events 统一进度流+token 打字机 |
| 只能命令行跑 | FastAPI 4 接口（SSE 发起/查状态/恢复/取结果）|
| 依赖本机环境、难交付 | Dockerfile 分层构建 + Compose 一体化编排 |

## 4. 核心设计决策（"你为什么这么设计"）
1. **为什么基于模板改不从零写**：模板 prompt 和职责划分经过验证，从零易在提示词工程踩坑；基于改造把精力放架构，也体现读懂现有代码再扩展。
2. **为什么拆双子图**：检索/打磨职责分离；子图临时字段不污染主图；子图多次 per-invocation 调用互不串味；可复用。
3. **critique 为什么放主图不放子图**：它是路由门卫，要同时决定回检索还是进打磨，必须站在能看全局的主图层；塞子图里看不到全局且和内部循环混淆。
4. **为什么要逃逸**：区分"写得差（重写能解决）"和"没素材（重写解决不了，只能补检索）"，避免内层空转烧 token。
5. **为什么持久化**：人在回路要 interrupt 暂停等人，没 checkpoint 进程一退状态就没了，无法跨请求恢复。
6. **为什么固定工作流不用 tool calling**：流程确定，固定编排更稳定可控可调试；tool calling 适合路径不固定、需模型临场决定调什么的开放场景。

## 5. 高频追问 · 标准答案

**Q1 两个评审为什么分开？** 信息完整性和写作质量是两类问题、解法不同：缺信息回检索补素材，写得差才重写；混一起会"没素材还反复重写"空转。

**Q2 双层循环计数变量、在哪递增、为什么、上限？**
- 外层 `loop_count`，在检索子图 **merge 节点**递增：merge 是双路唯一 fan-in 汇合点、一轮只经过一次，不会因并行重复计数；上限 3。
- 内层 `rewrite_count`，在 **write_report 重写时**递增、首次撰写不计数；上限 2。
- 到上限条件边强制往下走兜底。原则：**信号定路径、计数防死循环**；计数放在"动作真正完成一次"的节点。

**Q3 主图 State 放哪些字段、为什么不全放？**
- 主图只放跨阶段流转、条件边要用、要持久化的：topic、report、loop_count、is_passed、quality_passed/needs_more_info/quality_feedback、human_feedback 等约 9 个。
- 子图内部临时字段（search_queries、web_results、rag_results、scraped_urls、rewrite_count）只放子图 State。
- 好处：主干清晰、子图多次调用不污染、主图快照更轻、子图可复用。
- 机制：子图 State 字段可更多，主图只把**同名字段**传入、子图也只把主图同名字段更新回去。

**Q4 "子图字段被过滤"的坑？** 现象是 web_search 搜到了但 merge 收到 web_results=0、不报错。原因是框架按 State schema/类型注解白名单过滤，主图没声明的子图字段被静默丢掉。修复：节点入参统一用 `Dict[str, Any]` 宽类型接收，条件边补字段判断和兜底。教训：不报错但数据消失的 bug 最难查。

**Q5 checkpoint 存哪、怎么区分会话和子图？** PostgreSQL，三元主键 `thread_id`（哪次会话）+ `checkpoint_ns`（主图为空串、子图是 uuid）+ `checkpoint_id`（哪个超步快照）。SQL：`SELECT checkpoint_ns, COUNT(*) FROM checkpoints WHERE thread_id='x' GROUP BY checkpoint_ns`（注意没有 created_at，时间在 metadata jsonb）。

**Q6 interrupt 时程序什么状态？怎么恢复？意见怎么进去？**
- interrupt 不是阻塞死等，是存快照后**正常结束本次执行、释放资源**，返回"停在 human_review"。
- 每个超步后 Checkpointer 把完整 state+next 序列化入库。
- 用户回来带 thread_id 发 `Command(resume=意见)`，框架读最新快照，把 resume 值作为 interrupt() 的返回值注回节点，写进 state，条件边据此路由（意见回打磨、通过结束）。
- 类比：存档退出→选槽读档→带着选择从存档点继续。

**Q7 tool calling 完整流程？和固定工作流区别？**
bind_tools 给模型工具名+描述+参数 schema → 模型在 AIMessage 里返回 tool_calls（只给意图和参数，**不执行**）→ 代码执行工具、把结果封成 ToolMessage（带 tool_call_id）回灌 → 再请求模型，循环到无 tool_calls 出最终答案。固定工作流=开发者写死控制流（确定/可控/省钱）；tool calling=模型运行时动态决策（灵活/不可预测/可能多轮烧 token）；即 workflow vs agent。

**Q8 双路检索怎么并、怎么合？** plan 后 fan-out 成 web、rag 两条并行支路，fan-in 到 merge 做来源打标、URL 去重、素材裁剪上限。

**Q9 流式怎么实现、怎么只打报告正文？** astream_events 统一拿节点 start/end 和模型 token 事件，用 `event["metadata"]["langgraph_node"]` 过滤，只输出 write_report 节点 token；中断时当前流结束，恢复是另开新流。同步节点由 LangGraph 丢线程池执行，不阻塞事件循环，所以同步代码也能流式。

**Q10 外部依赖挂了会不会崩？怎么兜底？** 见第 7 节。

**Q11 为什么节点参数用 Dict[str, Any]？** 同 Q4，避免子图字段被窄类型注解按 schema 过滤。

**Q12 per-invocation 和默认持久化区别？** 子图每次调用全新状态（检索每轮从头来），默认模式会在多次调用间复用内部状态；checkpoint 都存同一个 PostgreSQL，用 checkpoint_ns 区分。

## 6. 工程化 / 部署要点
- **FastAPI**：lifespan 启动时连 PG、setup 建表、编译图全局复用；4 接口：POST /research/stream(SSE 发起)、GET /research/state/{tid}、POST /research/resume(恢复)、GET /research/result/{tid}。
- **ASGI/uvicorn**：FastAPI 是框架，uvicorn 是把它跑起来监听端口的 ASGI 服务器；容器内绑 0.0.0.0 外部才可达。
- **Docker**：Dockerfile 分层（依赖前置利用缓存）、两轮 pip 解决 langchain 要求 langgraph<1.2.0 与 1.2.11 的冲突、.dockerignore 挡 .env/volumes；Compose 起 app+postgres+Milvus(etcd/minio)，服务名互联、healthcheck 等 PG healthy 再起 app、bind mount 持久化、PG 映射 5433 避让本机 5432。

## 7. 健壮性兜底（工程稳定性，核心思路）
总原则：**节点内部消化异常、优雅降级，单点故障不扩散，部分可用好过整体报错。**
- web_search：多查询词逐个调，单条限流/超时跳过、全失败返回空；
- rag_search：Milvus/embedding 异常返回空，靠 web 路兜底（双路冗余）；
- scrape：单篇抓正文失败跳过、退回用搜索摘要，全失败全用摘要；
- write_report：素材再少也把 topic 给模型先出一版并标注资料局限；
- 上限保护：素材条数上限防爆 token、循环上限防死循环；
- 软硬依赖区分：搜索/抓取是软依赖可降级；PostgreSQL checkpointer 是硬依赖，挂了持久化无从谈起，让它快速失败、报错明确。
- 多级回退链：网页正文 → 搜索摘要 → 仅 topic+模型知识。

## 9. 容易说错被抓的点（避坑清单）
- critique 在**主图**，不是和子图并列、也不在打磨子图里。
- Tavily 只搜索（标题/摘要/URL），**抓正文是 scrape**；抓不到正文才用 Tavily 摘要兜底。
- loop_count 在 **merge**、rewrite_count 在 **write_report 重写时**（首次不计数）。
- 模型**不执行**工具，只返回 tool_calls，执行在外部代码、ToolMessage 回灌。
- thread_id 只是三元主键之一，存的是完整 state 快照不是只存 id。
- EXPOSE 不放行端口，真正对外靠 -p/ports；服务要绑 0.0.0.0。
- 容器内 localhost 指容器自己；连兄弟容器用服务名、连宿主机用 host.docker.internal。
- 别说是"完全从零自研"，是基于官方模板做架构级改造。
