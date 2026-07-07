# Enterprise Customer Agent

企业智能客服 Agent 平台示例项目，包含 Java 业务服务、Python AI Agent 服务、Vue 客户端/坐席端页面、Streamlit Demo 和项目文档。

## 项目结构

```text
enterprise-customer-agent/
├── business-service/   # Spring Boot 模拟核心业务系统
├── ai-agent-service/   # FastAPI + Agent + RAG + 工具调用
├── frontend-vue/       # Vue 客户端、调度端、坐席端界面
├── frontend-demo/      # Streamlit 客户侧演示入口
├── staff-console/      # Streamlit 坐席端演示入口
├── docs/               # PRD、接口、数据库和流程文档
└── README.md
```

## 本地启动

### 1. 启动 Java 业务服务

```bash
cd business-service
mvn spring-boot:run
```

默认端口：`8081`。

### 2. 启动 AI Agent 服务

```bash
cd ai-agent-service
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

默认端口：`8000`。

### 3. 启动 Vue 前端

```bash
cd frontend-vue
npm install
npm run dev
```

## 当前能力

- 客户登录、订单查询、工单查询和工单进度展示。
- 智能客服意图识别、RAG 检索、规则咨询、操作步骤咨询、状态查询和动作申请槽位收集。
- 退货规则咨询与真实退货申请分流，避免把规则咨询误判为操作申请。
- 人工客服请求、人工排队状态、AI 与人工会话分离展示。
- 坐席端待接入会话、工单状态和人工处理入口。
- 聊天消息支持 Markdown 展示，长回复可按标题、段落和列表渲染。

## 常用检查命令

```bash
cd ai-agent-service
.\.venv\Scripts\python.exe -m unittest tests.test_conversation_context tests.test_return_goods_policy_guardrail tests.test_ticket_progress_tools tests.test_rag_engineering
.\.venv\Scripts\python.exe -m compileall agents graphs rag schemas tools repositories app.py
```

```bash
cd frontend-vue
npm run build
```

## 架构边界

Java `business-service` 是核心业务系统。Python Agent 不直接写核心业务表，订单、工单、物流等业务动作必须通过 Tool 调用 Java 接口完成。

客户身份以 Java 登录 Token 为准。客户侧页面只展示安全话术、处理进度、工单号、工单状态和客户自己的会话消息，不展示内部风险原因、工具调用结果或 Agent 分析 JSON。

## 安全原则

- AI 不直接执行退款、赔付、删除、关闭投诉等高风险动作。
- 投诉、争议、赔付、法律风险和低置信度场景进入人工审核或人工处理。
- 所有关键 Agent 决策、工具调用和人工处理动作应记录日志，便于复盘和调试。
