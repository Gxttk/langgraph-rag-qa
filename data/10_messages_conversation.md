# 消息类型与多轮对话管理

## 四类消息

LangChain 把对话中的每句话抽象成消息对象，常见四类：

- HumanMessage：用户输入。
- AIMessage：大模型的回复，可能携带 tool_calls（工具调用意图）。
- SystemMessage：系统提示，设定角色、约束和输出格式，通常只在最前面放一条。
- ToolMessage：工具执行结果，必须带 tool_call_id 与对应的工具调用配对。

## messages 状态与 add_messages

多轮对话一般在 State 里用一个 messages 字段保存历史消息列表。更新列表必须指定 reducer，否则新返回的消息会整体覆盖历史：

- 用 operator.add 或 add_messages 做 reducer，新消息才会追加而不是覆盖。
- add_messages 会按消息 id 去重：新 id 追加、已存在的同 id 覆盖，因此人在回路里修改某条消息也不会重复堆叠。
- MessagesState 是预置好 messages 字段和 add_messages reducer 的 State，实际项目通常继承它再扩展自己的字段。

## 多轮对话与问题补全

单轮问答每次请求互相独立；多轮对话要结合历史理解当前问题。用户常说省略句或指代（“它怎么用”“再详细点”），需要先做问题补全（指代消解）：结合历史把残句改写成意思完整的独立问题。检索时用补全后的问题保证召回，生成时再结合完整历史保证回答连贯。

## 会话隔离

不同用户、不同会话之间不能串历史。LangGraph 用 config 里的 thread_id（等价于 session_id）区分会话，配合 Checkpointer 让每个会话各自维护一条消息历史，互不干扰。

## 上下文窗口管理

历史消息会越积越多，超过模型上下文窗口就要管理，常见手段：只保留最近若干轮、对更早的历史做摘要、用 RAG 把长期知识放进向量库按需检索（上下文是桌面、向量库是书架）。短期记忆靠 messages + checkpoint，长期知识记忆靠向量库检索。
