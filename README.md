# Multi-Agent 智能客服系统

FastAPI + LangGraph + pgvector 的智能客服系统：商城页下单，聊天页与 AI 客服对话；
Agent 能查订单/商品/退款、用 RAG 回答售后政策，并在拿不准时转人工。

## 快速开始

### 三个页面

| 页面 | 地址 | 用途 |
|---|---|---|
| 商城 | http://127.0.0.1:8000/static/index.html | 浏览商品、下单、看订单 |
| 聊天 | http://127.0.0.1:8000/static/chat.html | 用户与 AI 客服对话（SSE 流式） |
| 客服工作台 | http://127.0.0.1:8000/static/staff.html | 客服/主管/管理员处理转人工会话 |

### 启动（Docker，推荐）

```bash
docker compose build app
docker compose up -d --force-recreate app
curl http://127.0.0.1:8000/health/ready
```

### 启动（本地）

```bash
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\python main.py
```

两种方式**互斥**：Docker 跑着的时候不要再起本地进程，否则 8000 端口被旧进程占住，
出现"改了代码没生效"的假象。改了 `.env` 必须 `docker compose up -d --force-recreate app`，
env_file 只在创建容器时注入。

### 演示账号（密码均为 demo123）

| 用户名 | 角色 | 能力 | 登录页 |
|---|---|---|---|
| demo_user | 普通用户 | 下单、和 AI 客服对话、发起退款/转人工 | 商城 / 聊天页 |
| demo_cs | 客服员 | 看所有会话与订单、人工回复用户；不能批退款 | 工作台 |
| demo_leader | 主管 | 客服全部能力 + 审批退款/补偿券 | 工作台 |
| demo_admin | 管理员 | 主管全部能力 + checkpoint 巡检 | 工作台 |

退款是"批钱"，只给 leader/admin（`_execute_refund_decision` 硬校验），客服只能回复。

### 3 分钟演示动线

1. demo_user 在商城下单（如无线充电器 129 元）。
2. 聊天页对 AI 说"订单 xxx 不想要了，退了吧" → 未发货且在限额内 → 自动退款
   （需 `WRITE_ACTIONS_MODE=auto`；超限或已发货仍转人工，规则见 `policies/refund_policy.py`）。
3. 聊天里说"我要举报" → 会话进待人工队列，用户侧显示"已转人工"。
4. 另开浏览器登 demo_admin → 工作台 → 待人工队列点开会话 → 回复；
   若有待审批动作则通过/驳回。
5. 切回用户聊天页：人工回复 8 秒内出现，会话退出待人工队列。

## 技术栈

- Python 3.13 / FastAPI / Uvicorn
- PostgreSQL 18 + pgvector（SQLAlchemy async + psycopg3）
- LangGraph + LangChain ChatOpenAI（DeepSeek 兼容接口）
- DashScope Embedding（维度由 `EMBEDDING_DIM` 配置）
- SSE 流式、JWT + bcrypt
- Alembic 迁移、Docker Compose

## 配置

复制 `.env.example` 为 `.env` 并填真实值。必填项：

| 变量 | 说明 |
|---|---|
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | PostgreSQL 连接 |
| `SECRET_KEY` | JWT 签名密钥，生成：`python -c "import secrets; print(secrets.token_hex(32))"` |
| `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` | 对话模型 |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | 向量模型 |
| `EMBEDDING_DIM` | 与 embedding 模型实际输出维度一致，启动时会校验 |

其余变量（重排、Query 改写、记忆、限流、写动作挡位等）在 `.env.example` 里都有，
不配则走代码默认值。

## 建库与迁移

```sql
-- PostgreSQL 里先建库 + 库级扩展
CREATE DATABASE customer_service_db;
\c customer_service_db
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;
```

空库直接跑迁移，由 Alembic 建全量表：

```bash
.venv\Scripts\alembic.exe upgrade head
```

已有表但没有 `alembic_version` 的老库，先标记初始迁移再升级：

```bash
.venv\Scripts\alembic.exe stamp b434e1b0cd3b
.venv\Scripts\alembic.exe upgrade head
```

灌种子数据：

```bash
.venv\Scripts\python seed_data.py
```

## 冒烟验证

```bash
# 存活 + 依赖就绪（期望 vector:"ok(count=N)"）
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/health/ready

# 登录拿 token
curl -s -X POST http://127.0.0.1:8000/auth/login -F username=demo_user -F password=demo123

# 调一次 Agent（<TOKEN> 换成上一步的 access_token）
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"查一下订单 20260816001 的状态\",\"session_id\":null}"
```

## 架构

### Agent 编排

单 Agent 用 LangChain `create_agent` 搭建，4 个框架原生钩子函数（全 async）：

| 钩子 | 挂载点 | 作用 |
|---|---|---|
| `rag_hook` | `@before_model` | 入口检索，政策类问题必检索 |
| `model_step_log_hook` | `@wrap_model_call` | 步骤日志，拿 usage 与决策 |
| `forced_final_answer_hook` | `@wrap_model_call` | 达到步数上限后强制基于已有信息收尾 |
| `tool_logging_hook` | `@wrap_tool_call` | 工具日志 + 异常归一化 |

装饰器返回的就是 `AgentMiddleware` 实例，与官方件同列 `middleware=[...]`，没有自写中间件类。

几处没有用官方件的地方：官方的 `ToolCallLimitMiddleware.exit_behavior='end'` 会硬停止、
让用户丢掉前面已查到的数据，所以收尾逻辑自己写；工具层需要记录归属与耗时，
官方件没有工具级日志。

**LangChain 管授权层**（`create_agent`、钩子装饰器、`@tool`），
**LangGraph 管运行时**（`StateGraph` 路由、`add_node` 挂子图、`add_messages` reducer、
`InjectedState` 身份注入）。全链路 async，没有 `asyncio.to_thread` 兜底。

身份注入走 `InjectedState`，从可信 State 读 `user`，不由 LLM 参数传入。

### 记忆

| 记忆 | 引擎 | 键 | 生命周期 |
|---|---|---|---|
| 短期（会话内多轮） | LangGraph `AsyncPostgresSaver` | `thread_id = sess-{session_id}` | 会话内；删会话时一起清 |
| 长期（跨会话用户事实） | LangGraph `AsyncPostgresStore` | `namespace = ("memories", str(user_id))` | 跨会话、用户级 |
| 聊天历史 | 业务表 `chat_sessions`/`messages` | `session_id` | 每轮显式落库 |

- **写**：每轮回答落库后，只从用户消息抽取稳定事实，用提示词约束 JSON 加容错解析。
  不用 `with_structured_output`：本项目模型思考模式不支持强制格式，一调就 400。
  失败只记日志，不影响回答。
- **读**：每轮调 Agent 前取最近 N 条，**只改系统提示**，不进 `messages`/checkpoint。
- **窗口**：`history_window_hook` 只裁送进模型的最近 `CHECKPOINT_MESSAGE_WINDOW` 条，
  不切断 tool_call/ToolMessage 配对，图状态完整保留。
- **会话管理**：`POST /chat/sessions` 建空会话、`DELETE /chat/sessions/{id}` 仅所有者
  （越权统一 404 防探测），列表带 `title`/`message_count`/`is_owner`。

### 编排形态

默认主管路由式 Multi-Agent：主管节点按意图路由到订单、商品、政策三个领域助手，再执行工具。
意图置信度低于阈值时转澄清而非直接执行，超出客服范围的问题直接拒答。

需要回落单 Agent（单模型持有全部工具）时：

```dotenv
USE_SUPERVISOR=false
```

`USE_TODO_PLANNER=true` 时，多步任务先 `write_todos` 外化计划，再逐步执行。

### 可选能力

以下能力默认关闭，开启方式见 `.env.example`：

- **重排门控**（`RERANK_ENABLED=1`）：融合排序后取前 N 条送 cross-encoder，
  低于阈值视为无命中并拒答。候选数是常量，改它必须重新标定阈值。
- **Query 改写**（`QUERY_REWRITE_ENABLED=1`）：检索前用 LLM 把"那二类城市呢"这类
  依赖上下文的问题补全成自足查询；改写结果只用于检索，超时/异常一律回退原 query。

## 目录结构

| 路径 | 职责 |
|---|---|
| main.py | App 组装、CORS、startup fail-fast、RAG 初始化 |
| config/ | 数据库/模型/日志配置、写动作阈值 |
| agents/ | `create_agent` 单 Agent + Supervisor 多 Agent 子图 |
| tools/ | 订单/商品/退款工具（真调本系统 HTTP） |
| rag/ | pgvector 向量库：分块、检索、重排、Query 改写 |
| routers/ | auth / products / orders / refunds / coupons / chat / tickets / stats |
| core/ | JWT、权限依赖、限流、安全响应头 |
| crud/ | 数据库读写与订单事务 |
| models/ | SQLAlchemy ORM 模型 |
| policies/ | 退款等写动作的规则层与动作注册表 |
| services/ | 审批、写动作执行、记忆、工单 |
| alembic/ | 数据库迁移 |
| static/ | 商城页 + 聊天页 + 客服工作台 |
| knowledge/ | 知识库文本（启动时逐行读取并向量化） |

## 测试

```bash
.venv\Scripts\python -m pytest -q
```

## 评测

单元测试覆盖基础逻辑；另有 Agent 七维评测和客服策略评测，用于检查工具真实性、权限和策略边界。详见 `eval/README.md` 和 `eval/cs_rsi/README.md`。

## 设计要点## 设计要点

1. **归属校验双层防线**：路由层校验 owner/角色，工具层再用 `InjectedState` 注入当前用户，
   普通用户无法借 Agent 越权。
2. **写后立读**：关键写接口在响应前显式 commit，避免依赖注入的提交时机造成读写竞态。
3. **写动作分级**：`off` 不注册工具 / `shadow` 一律转人工确认 / `auto` 规则全过才自动执行，
   逐动作可用 `ACTION_<NAME>_MODE` 覆盖，"拿不准就转人工"是默认姿态。
4. **限流**：账号 + IP 双维度，429 带 Retry-After；实现可替换，单进程内存版，
   多 worker 时换 Redis 后端。
5. **健康检查**：liveness 与 readiness 分离，readiness 真查数据库/pgvector/模型 key。
6. **可观测性**：`request_id` 串起 HTTP → Agent → 工具 → SSE，关键业务操作带业务单号。
7. **迁移**：Alembic 管 schema，容器入口自动 upgrade head，不在启动时 create_all。
8. **容器化**：非 root 运行，compose 编排 app 与 db。
