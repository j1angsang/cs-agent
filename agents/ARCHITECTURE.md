# 单 Agent 编排

`create_agent` + 4 个框架原生钩子函数，没有自写中间件类。

## 中间件注册顺序

`_build_middleware()` 的注册顺序就是执行与包裹层级：

```
用户消息 -> run_agent(async, ainvoke)
  -> @before_model rag_hook            先注入 RAG 知识（[知识库参考]，本轮已注入则跳过）
  -> 模型调用洋葱（wrap_model_call，先注册的在外层）:
       model_step_log_hook(外) -> forced_final_answer_hook(内) -> 模型
       forced_final: ToolMessage 数 >= MAX_STEPS 时 override(tools=[]) 强制收尾
       step_log:     记录 agent.step 与 usage
  -> @wrap_tool_call tool_logging_hook  tool.execute 日志 + 异常归一化
  -> 工具经 InjectedState 读可信 state["user"]，身份不来自 LLM 参数
USE_TODO_PLANNER=true 时在列表最前插官方 TodoListMiddleware
```

## 旧类 → 新钩子

| 旧自写中间件类 | 新钩子 | 为什么不用官方件 |
|---|---|---|
| `RagMiddleware`(before_model) | `@before_model rag_hook` | 官方 16 件里没有检索/RAG 组件 |
| `AgentStepLoggingMiddleware` | `@wrap_model_call model_step_log_hook` | 没有步骤/token 日志件 |
| `ForcedFinalAnswerMiddleware` | `@wrap_model_call forced_final_answer_hook` | `ToolCallLimitMiddleware.exit_behavior` 只有 continue/error/end，没有"超限后强制收尾"档 |
| `ToolLoggingMiddleware` | `@wrap_tool_call tool_logging_hook` | 没有工具级日志件，且重试层次不对 |

分层：授权层（`create_agent`、钩子装饰器、`@tool`）用 LangChain；
运行时（supervisor 的 `StateGraph` 路由、`add_node` 子图、`add_messages`、`InjectedState`）用 LangGraph。
全链路 async，没有 `asyncio.to_thread` 兜底。
