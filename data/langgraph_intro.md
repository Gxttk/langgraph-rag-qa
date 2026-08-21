# LangGraph 核心概念

## 什么是 LangGraph

LangGraph 是 LangChain 团队推出的一个用于构建有状态、多参与者应用程序的库。它基于图结构来编排 LLM 应用，支持循环、条件分支、持久化状态等高级特性。

## 核心概念

### State（状态）
State 是图中所有节点共享的数据结构。每个节点接收当前 State，执行逻辑后返回要更新的字段，LangGraph 自动合并到全局 State 中。State 可以是 TypedDict 或 Pydantic 模型。

### Node（节点）
Node 是图中的执行单元，本质上是一个 Python 函数，接收 State 作为输入，返回字典作为 State 的更新。节点可以是同步函数也可以是异步函数。

### Edge（边）
Edge 定义节点之间的执行路径。分为普通边（无条件跳转）和条件边（根据 State 中的字段动态决定下一个节点）。

### Checkpoint（检查点）
Checkpoint 是 LangGraph 的持久化机制。每次节点执行完成后，当前 State 会被保存到 CheckpointSaver 中，支持中断恢复、时间旅行调试等功能。

## 子图（Subgraph）

子图是将一个完整的图作为另一个图的节点使用。子图有自己独立的 State，通过同名字段与父图传递数据。子图可以实现模块复用和状态隔离。

## 人在回路（Human-in-the-loop）

LangGraph 通过 interrupt() 函数实现人在回路。当节点调用 interrupt() 时，图执行暂停，等待外部输入。通过 Command(resume=值) 恢复执行，interrupt() 返回该值。

## 流式输出

LangGraph 支持多种流式输出方式：
- astream：图状态流，每个节点完成后输出 State 更新
- astream_events：事件流，包含节点开始/结束、LLM token 等细粒度事件
- stream_mode：可以选择 values、updates、debug 等模式
