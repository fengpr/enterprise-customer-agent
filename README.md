# Enterprise Customer Agent

企业智能客服受控 Agent 平台。项目采用 Vue、FastAPI、LangChain/LangGraph、RAG、Redis Stream 与 Spring Boot，覆盖客户咨询、订单/物流查询、多轮售后、工单流转、人工接管、异步评测和运行监控。

当前定位是：**可运行的企业客服 Agent 原型 + 电商领域实现**。项目适合作为开发演示、作品集和内部测试基础，但尚未完成真实生产上线所需的全部安全、容量和部署验收，不能宣称已经支持 100 QPS 或可直接承载真实客户流量。

## 核心设计

```text
Vue 客户端 / 坐席工作台
          │
          ▼
FastAPI Agent API
  鉴权、限流、入队、SSE、结果查询
          │
          ▼
Redis Stream Consumer Group
  幂等、ACK、Pending 恢复、重试、DLQ
          │
          ▼
独立 Agent Worker
          │
          ▼
AgentExecutionService
  会话记忆、LangGraph、RAG、工具调用、Trace
       │                       │
       ▼                       ▼
pgvector / 知识库       Spring Boot 业务服务
                         认证、订单、物流、工单

线上脱敏 Trace ──► 独立 DeepEval Worker ──► 评测结果与失败诊断
```

本项目采用“LLM 语义理解 + 后端确定性编排”的受控 Agent 架构：LLM 负责意图识别、槽位抽取和回复生成；订单归属、权限、风险、幂等和工具执行由后端规则、状态机与 LangGraph 工作流控制。

## 已实现能力

- 客户登录、订单列表、订单详情、物流轨迹、工单列表和处理进度。
- RAG 知识检索、知识库版本缓存、引用 ID、必要事实覆盖和无依据断言检查。
- 退货规则咨询与真实退货动作分流，避免知识问答误触发业务写操作。
- 多轮 `pending_action` 与动态槽位编排，支持一次性提供、乱序补充、取消和超时确认。
- 当前会话记忆、登录身份优先级和用户自称姓名冲突保护。
- Redis Stream 可靠任务队列、Worker 心跳、结果状态查询和 SSE 增量事件。
- LLM 与 Java 工具统一超时、重试、熔断、并发舱壁和安全降级。
- 工单幂等创建、人工接管、坐席处理和客户侧进度闭环。
- DeepEval 黄金集回归与线上脱敏 Trace 异步评测。
- Prometheus `/metrics`、OpenTelemetry Trace 和内部系统监控页面。

## 项目结构

```text
enterprise-customer-agent/
├── business-service/        # Spring Boot 核心业务服务
├── ai-agent-service/        # FastAPI、Agent、RAG、队列、Worker、评测
│   ├── agents/              # 意图、对话上下文、动作状态机
│   ├── graphs/              # LangGraph 业务工作流
│   ├── rag/                 # 检索、入库、引用、评测与 Worker
│   ├── tools/               # 订单、物流、工单受控工具
│   ├── repositories/        # 会话、日志与评测仓储
│   └── services/            # 执行服务、队列、缓存、韧性与可观测性
├── frontend-vue/            # 当前主要前端
├── frontend-demo/           # 早期 Streamlit 客户端演示
├── staff-console/           # 早期 Streamlit 坐席端演示
├── load-tests/              # k6 混合场景压测脚本
├── deploy/                  # Kubernetes 资源草案
├── docs/                    # 架构、接口、迁移、评测和压测资料
├── start-all.bat            # Windows 本地一键启动入口
└── start-all.ps1            # 启动编排脚本
```

## 本地快速启动

### 前置依赖

- JDK 17 和 Maven 3.9+
- Python 3.13 与项目虚拟环境
- Node.js 20+ 和 npm
- Docker Desktop，用于本地 Redis 与 pgvector/PostgreSQL

首次运行前安装依赖：

```powershell
cd ai-agent-service
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

cd ..\frontend-vue
npm install
```

在 `ai-agent-service/.env` 中配置模型和嵌入服务密钥。不要提交真实 API Key、Token 或客户数据。

完成依赖准备后，在仓库根目录运行：

```bat
start-all.bat
```

脚本会尝试启动 Redis、pgvector/PostgreSQL、Java 业务服务、Agent API、独立 Agent Worker 和 Vue 前端。

启动后可访问：

- Vue 前端：`http://localhost:5173`
- Agent 健康检查：`http://localhost:8000/health`
- Agent 指标：`http://localhost:8000/metrics`
- Java 健康检查：`http://localhost:8081/actuator/health`
- Java 指标：`http://localhost:8081/actuator/prometheus`
- RAG 评测页：`http://localhost:5173/staff/rag-evaluation`
- 系统监控页：`http://localhost:5173/staff/system-monitor`
- 客户站内通知：`http://localhost:5173/customer/notifications`

如果 Docker、Java 或 Agent API 启动失败，脚本不会继续启动前端，避免页面持续出现 `ECONNREFUSED`。

## 手动启动

### 1. Java 业务服务

```powershell
cd business-service
mvn spring-boot:run
```

### 2. Agent API

```powershell
cd ai-agent-service
$env:REDIS_URL="redis://127.0.0.1:6379/0"
$env:AGENT_EXECUTION_QUEUE_ENABLED="true"
.\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000
```

### 3. 独立 Agent Worker

```powershell
cd ai-agent-service
$env:REDIS_URL="redis://127.0.0.1:6379/0"
$env:AGENT_EXECUTION_QUEUE_ENABLED="true"
.\.venv\Scripts\python.exe -m rag.agent_execution_worker
```

### 4. 定时物流复核 Worker

```powershell
cd ai-agent-service
$env:REDIS_URL="redis://127.0.0.1:6379/0"
.\.venv\Scripts\python.exe -m rag.scheduled_followup_worker
```

该 Worker 使用独立 Redis Stream。到期任务只重新校验订单归属、查询最新物流、写回原会话并创建站内通知，不会自动创建退货或退款工单。

### 5. Vue 前端

```powershell
cd frontend-vue
npm run dev
```

### 6. DeepEval Worker（按需启动）

```powershell
cd ai-agent-service
.\.venv\Scripts\python.exe -m rag.evaluation_worker --interval 2
```

DeepEval Worker 与在线 Agent Worker 必须保持独立，客户请求只采集脱敏 Trace，不同步等待 LLM Judge。

## 关键配置

| 环境变量 | 作用 | 本地默认/要求 |
|---|---|---|
| `DEEPSEEK_API_KEY` / `LLM_API_KEY` | 在线 LLM 凭证 | 使用真实模型时必填 |
| `LLM_BASE_URL`、`LLM_MODEL` | OpenAI 兼容模型地址与模型名 | 按供应商配置 |
| `REDIS_URL` | 队列、事件流和缓存 | `redis://127.0.0.1:6379/0` |
| `AGENT_EXECUTION_QUEUE_ENABLED` | 启用 Redis Stream 在线队列 | 生产应为 `true` |
| `BUSINESS_SERVICE_URL` | Java 业务服务地址 | `http://localhost:8081` |
| `DB_PROVIDER` | Python Repository 数据库 | 本地默认 `sqlite`，生产目标为 `postgres` |
| `DATABASE_URL` | Python 会话、日志和评测 PostgreSQL DSN | `DB_PROVIDER=postgres` 时必填 |
| `RAG_STORE_BACKEND` | RAG 后端 | 本地默认 `memory`，生产目标为 `pgvector` |
| `RAG_DATABASE_URL` | pgvector 数据库 DSN | `RAG_STORE_BACKEND=pgvector` 时必填 |
| `RAG_STRICT_STARTUP` | RAG 初始化失败时是否禁止静默回退 | 生产应为 `true` |
| `RAG_LLM_QUERY_REWRITE_ENABLED` | 启用知识咨询 LLM 查询重写 | 默认 `false`，需显式启用 |
| `RAG_QUERY_REWRITE_PROVIDER`、`RAG_QUERY_REWRITE_MODEL` | 独立轻量重写模型供应商与模型名 | 启用时必填 |
| `RAG_QUERY_REWRITE_API_KEY`、`RAG_QUERY_REWRITE_BASE_URL` | 独立重写模型凭证与兼容地址 | 不复用在线回复模型凭证 |
| `RAG_QUERY_REWRITE_TOTAL_TIMEOUT` | 查询重写总超时 | 默认 `3` 秒，失败自动规则降级 |
| `AGENT_INTERNAL_SECRET` | Python 与 Java 内部接口密钥 | 生产必须注入强随机值 |
| `AGENT_EXECUTION_SECRET` | Worker 短期执行凭证签名密钥 | 生产必须注入强随机值 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP Collector 地址 | 未配置时只创建本地 Span |

本地默认使用 SQLite Repository 和内存检索，便于快速演示。生产环境不能依赖这些默认值。

## 架构与安全边界

- Java `business-service` 是核心业务系统，Python Agent 不直接写订单、物流或工单业务表。
- 客户身份以 Java 登录态为准，前端传入的 `customer_id`、姓名或订单号不能作为授权依据。
- 队列中不保存客户原始 Authorization Token，Worker 使用短期内部执行凭证调用 Java。
- 创建工单等写操作必须携带幂等键，并在 Java 侧再次校验客户与订单归属。
- AI 不直接执行退款、赔付、删除、关闭投诉等高风险动作。
- 投诉、争议、法律风险、低置信度和下游异常进入人工审核或安全降级。
- 客户侧不展示 Prompt、风险原因、工具原始结果、内部建议或 Trace 调试字段。

## 测试与检查

Python 全量测试：

```powershell
cd ai-agent-service
.\.venv\Scripts\python.exe -m pytest tests
```

Python 语法检查：

```powershell
cd ai-agent-service
.\.venv\Scripts\python.exe -m compileall agents graphs rag schemas tools repositories services app.py
```

Java 测试：

```powershell
cd business-service
mvn test
```

前端类型检查和构建：

```powershell
cd frontend-vue
npm run typecheck
npm run build
```

最近一次本地审计（2026-07-17）：

- Python：203 项通过、1 项失败，另有 22 个子测试通过；当前失败是会话历史工单催办路由回归。
- Java：6 项通过。
- Vue：类型检查通过。
- 10/20/50/100 QPS 尚未完成真实分阶段压测。

因此当前版本不能打生产 Release Tag。修复回归后还需要完成 PostgreSQL/Redis 集成、端到端测试、故障注入和容量验收。

## 生产上线前必须完成

1. 移除 Demo 账号、明文密码和硬编码 JWT/内部密钥，接入正式身份系统与 Secret 管理。
2. 真正打通 Python Repository PostgreSQL 方言、持久连接池、迁移执行和回滚验证。
3. 修正生产 Compose/Kubernetes 中的 `DB_PROVIDER`、`DATABASE_URL`、持久卷、健康检查和完整服务资源。
4. 为 Redis Stream 增加消费后删除或裁剪、真实积压统计、延迟重试和数据留存策略。
5. 建立 CI 门禁，并增加真实 PostgreSQL、Redis、Controller/Auth 和浏览器 E2E 测试。
6. 按 10/20/50/100 QPS 分阶段执行压测和 LLM 429、超时、Java/Redis/Worker 故障注入。
7. 完成备份恢复、密钥轮换、部署回滚、灰度发布和告警演练。

容量现状与压测脚本说明见 [Agent 分阶段容量报告](docs/performance/agent_100qps_capacity_report.md)。

## 通用性与迁移方向

当前队列、SSE、会话、韧性、缓存、可观测性、Trace 和评测框架具有较高复用价值，但意图 Schema、售后状态机、工具 API、RAG 分类、Java DTO 和前端文案仍绑定当前电商业务。

迁移到另一个商城时，建议逐步抽象：

- `AgentKernel`：执行服务、队列、SSE、会话、韧性、Trace 和评测。
- `DomainPack`：意图、动作、槽位、风险策略、Prompt、工作流和领域评测集。
- `BusinessConnector`：身份、订单、物流和工单的统一接口与商城 Adapter。
- `ToolRegistry`：工具输入输出 Schema、读写属性、权限、幂等、超时和风险级别。
- `KnowledgePack`：商城独立知识库、版本、业务分类和黄金评测集。
- `/capabilities`：向前端下发支持的动作、字段和状态文案，减少页面硬编码。

真正的通用性验收标准是：接入第二个商城时不修改 `AgentKernel`，只新增 Connector、DomainPack、KnowledgePack 和必要的领域 UI 扩展。

## 相关文档

- [PRD](docs/PRD.md)
- [API 接口文档](docs/API接口文档.md)
- [DeepEval 评测架构](docs/DeepEval评测架构.md)
- [PostgreSQL 迁移说明](docs/postgres-migration.md)
- [可观测性说明](docs/observability/README.md)
- [高并发部署与压测](docs/高并发部署与压测.md)

## License

本项目使用 [MIT License](LICENSE)。
