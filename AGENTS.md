# 仓库贡献指南

## 项目结构与模块组织

本仓库按业务边界拆分：

- `business-service/`：Spring Boot 模拟核心业务系统，负责登录认证、客户、商品、订单、物流、工单和坐席接口。
- `ai-agent-service/`：Python FastAPI 服务，负责 LangChain/LangGraph Agent 流程、RAG、工具调用、会话、Trace、评测、可靠队列、Worker 和 Agent 日志。
- `frontend-vue/`：当前主要前端，包含客户自助入口、坐席工作台、调度端、RAG 质量评测页和内部系统监控页。
- `frontend-demo/`：早期 Streamlit 客户自助入口，仅作为演示或历史兼容，不再作为主要前端演进方向。
- `docs/`：PRD 摘要、API 接口文档、数据库设计、迁移说明、可观测性和性能压测资料。
- `deploy/`：Kubernetes 等部署资源。

Python 核心代码位于 `ai-agent-service/agents`、`graphs`、`rag`、`tools`、`schemas`、`repositories`、`services`。Java 代码位于 `business-service/src/main/java/com/example/business`。

## 当前架构边界

Java `business-service` 是核心业务系统，Python Agent 不允许越权直接写核心业务表。订单查询、物流查询、工单创建、工单状态流转、催单等业务动作必须通过 Tool 调用 Java 接口完成。

客户身份以 Java 登录 Token 为准。前端传入的 `customer_id` 不可信，Agent 服务必须通过 `Authorization: Bearer <token>` 校验当前用户。Redis 队列、Trace、日志和指标中不得保存 Authorization 原始 Token。

在线 Agent 请求默认通过 Redis Stream 可靠队列进入独立 `agent-worker` 执行。FastAPI API 进程负责鉴权、限流、参数校验、入队、SSE/响应编排和结果查询，不应绕过 `AgentExecutionQueue` 长时间同步执行生产 Agent。

`AgentExecutionService` 是 Agent 业务执行边界，负责会话构建、消息落库、RAG、工具调用、LLM 生成、人工接管判断、Trace 组装和结果组装。API 和 Worker 都应通过该 Service 执行 Agent，不要让 Worker 直接导入 `app.py` 复用路由函数。

DeepEval/RAG 评测链路必须保持异步后台执行。线上客户请求只采集脱敏 Trace，不允许在用户请求完成后同步调用 LLM Judge。

## 角色与前端展示边界

`frontend-vue` 是当前主要前端：

- 客户侧只展示客户安全话术 `customer_message`、处理进度 `service_status`、工单号、工单状态、订单关联信息、当前客户自己的会话和消息。
- 坐席工作台展示待处理工单、人工接管会话、AI 建议稿、坐席回复和工单处理操作。
- RAG 质量评测页位于 `/staff/rag-evaluation`，用于内部查看黄金集、真实 Agent 评测和线上采样质量。
- 内部系统监控页位于 `/staff/system-monitor`，用于查看 Worker 状态、Redis Stream 积压、Pending、DLQ、降级次数、LLM timeout/429/circuit_open 和缓存命中率。

客户侧不得展示 `risk_reasons`、`decision_type`、AI 分析 JSON、工具调用原始结果、`internal_suggestion`、Prompt、Trace 细节或审核按钮。

主管或管理员功能应单独扩展，不要混入客户侧页面。

## 当前优先级与演进方向

当前关键目标是保证客服 Agent 的稳定闭环和高并发基础能力：

客户提交问题 → Agent 判断 → 自动回复或建工单 → 坐席看到待处理工单/人工会话 → 坐席回复或更新状态 → 客户侧看到最新进度。

高并发改造的核心约束：

- 在线请求必须受队列、限流、超时、熔断和降级保护。
- Redis Stream 队列需要支持 ACK、Pending 恢复、失败重试、死信队列和幂等去重。
- LLM 与 Java 工具调用必须通过统一韧性封装，避免散落的直接 `requests/httpx` 调用。
- 写操作必须幂等，尤其是创建人工工单、创建/更新业务工单等路径。
- 生产监控以 Prometheus/Grafana 为权威；项目内 `/staff/system-monitor` 是轻量内部面板，不替代多副本生产大盘。

## 构建、测试与本地开发命令

推荐一键启动：

```bat
start-all.bat
```

`start-all.bat` 会调用 `start-all.ps1`，尝试启动：

- Redis：`redis://127.0.0.1:6379/0`
- pgvector/PostgreSQL：`localhost:5432`
- Java `business-service`：`http://localhost:8081`
- Python `ai-agent-service`：`http://localhost:8000`
- 独立 `agent-worker`
- Vue 前端：`http://localhost:5173`

本地一键启动依赖 Docker Desktop。若 Docker 报 `permission denied while trying to connect to the docker API at npipe:////./pipe/docker_engine`，需要启动 Docker Desktop，并确保当前 Windows 用户属于 `docker-users` 组，重新登录后再运行脚本。

手动启动 Java 业务服务：

```bash
cd business-service
mvn spring-boot:run
```

手动启动 Agent API：

```bash
cd ai-agent-service
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set REDIS_URL=redis://127.0.0.1:6379/0
set AGENT_EXECUTION_QUEUE_ENABLED=true
uvicorn app:app --reload --port 8000
```

手动启动 Agent Worker：

```bash
cd ai-agent-service
.venv\Scripts\activate
set REDIS_URL=redis://127.0.0.1:6379/0
set AGENT_EXECUTION_QUEUE_ENABLED=true
python -m rag.agent_execution_worker
```

手动启动 Vue 前端：

```bash
cd frontend-vue
npm install
npm run dev
```

早期 Streamlit 客户入口：

```bash
cd frontend-demo
streamlit run streamlit_app.py
```

Python 测试：

```bash
cd ai-agent-service
.venv\Scripts\python.exe -m pytest tests
```

Python 语法检查：

```bash
cd ai-agent-service
python -m compileall agents graphs rag schemas tools repositories services app.py
```

前端类型检查：

```bash
cd frontend-vue
npm run typecheck
```

## 编码风格与命名规范

所有文件统一使用 UTF-8。Python 使用 4 空格缩进，优先补充类型提示；函数和模块使用 `snake_case`，类和 Pydantic 模型使用 `PascalCase`。Java 遵循 Spring 常见约定：类名使用 `PascalCase`，方法和字段使用 `camelCase`，包路径统一放在 `com.example.business` 下。

Agent 流程职责要清晰：LangGraph 工作流节点放在 `graphs/`，LangChain 链路放在 `agents/` 或 `rag/`，外部业务系统调用放在 `tools/`，横切能力和基础设施封装放在 `services/`。

## 代码注释规范

编写、修改、重构代码时，必须为关键代码添加中文注释。新增代码同步补充注释；修改业务逻辑时同步更新旧注释；不要新增英文注释。

Java 使用 Javadoc 风格注释。核心类必须说明业务作用；Controller、Service、Mapper、Agent、Tool、State、Node 等结构需要说明其职责。核心方法必须说明方法作用；复杂方法需要说明入参、返回值和异常情况。

Python 使用中文 docstring。核心类、核心函数、Agent 节点、工具调用、RAG 链路、队列状态流转和降级逻辑都需要说明业务职责。

关键条件判断、状态流转、权限校验、数据库查询、Agent 节点流转、工具调用、队列 ACK/重试/DLQ、幂等创建等逻辑，需要添加简洁中文行内注释。注释要解释业务职责或设计原因，不要只写“处理数据”“执行逻辑”等空泛描述。

## 测试规范

新增 Python 测试请放在 `ai-agent-service/tests/`，使用 `pytest`，文件命名示例：`test_customer_service_agent.py`。Java 测试请放在 `business-service/src/test/java`。前端改动至少运行 `npm run typecheck`。

测试重点应覆盖：

- 登录鉴权和会话隔离；
- 意图识别、风险路由和人工接管；
- RAG 检索、引用、有据性和幻觉样本；
- 工具调用成功、失败、超时、熔断和降级；
- Redis Stream 入队、ACK、Pending 恢复、重试、DLQ 和幂等去重；
- `/api/agent/reply`、SSE、结果查询接口的一致性；
- 工单创建、状态更新和幂等人工降级；
- 系统监控、Prometheus 指标和敏感信息过滤。

## 提交与 Pull Request 规范

建议使用简洁的约定式提交信息：

```text
feat: add staff ticket console
fix: protect customer session access
docs: update contributor guide
```

Pull Request 需要包含变更摘要、影响模块、本地运行过的命令，以及与 PRD 或需求的对应关系。涉及前端页面变化时，请附截图或简短说明。涉及业务规则、Agent 流程、队列、降级或风险控制的改动，需要说明验证方式。

## 安全与配置建议

不要提交 API Key、模型凭证、客户隐私数据、Docker 私有登录配置或生成的虚拟环境。模型配置、RAG 配置、Redis、PostgreSQL 和业务服务地址应通过环境变量维护。

Agent 工具不得直接执行退款、赔付、删除、关闭投诉等高风险动作。高风险动作必须进入人工审核或 Java 业务系统权限控制。

日志、Trace、Redis 队列、Prometheus 指标和前端监控页面不得展示 Authorization、手机号、邮箱、地址、完整订单号、Prompt 或工具原始敏感返回。如需展示业务标识，必须脱敏或哈希。
