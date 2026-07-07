# 仓库贡献指南

## 项目结构与模块组织

本仓库按业务边界拆分：

- `business-service/`：Spring Boot 模拟核心业务系统，负责登录认证、客户、商品、订单和工单接口。
- `ai-agent-service/`：Python FastAPI 服务，负责 LangChain/LangGraph Agent 流程、RAG、工具调用、Schema、Prompt、会话和 Agent 日志。
- `frontend-demo/`：Streamlit 客户自助入口，展示客户可见回复、会话历史、工单号和处理进度。
- `docs/`：PRD 摘要、API 接口文档和数据库设计。

Python 核心代码位于 `ai-agent-service/agents`、`graphs`、`rag`、`tools`、`schemas`、`repositories`。Java 代码位于 `business-service/src/main/java/com/example/business`。

## 当前架构边界

Java `business-service` 是核心业务系统，Python Agent 不允许越权直接写核心业务表。订单查询、工单创建、工单状态流转等业务动作必须通过 Tool 调用 Java 接口完成。

客户身份以 Java 登录 Token 为准。前端传入的 `customer_id` 不可信，Agent 服务必须通过 `Authorization: Bearer <token>` 校验当前客户。

## 角色与前端展示边界

当前 `frontend-demo` 定位为客户侧自助入口，只展示：

- 客户安全话术 `customer_message`
- 处理进度 `service_status`
- 工单号、工单状态、订单关联信息
- 当前客户自己的会话和消息

客户侧不得展示 `risk_reasons`、`decision_type`、AI 分析 JSON、工具调用结果、`internal_suggestion` 或审核按钮。

后续应新增 `staff-console/` 作为客服坐席端，用于展示 AI 内部建议稿、风险原因、工具调用结果、客户上下文、人工回复和工单处理按钮。主管或管理员功能应再单独扩展，不要混入客户侧页面。

## 下一阶段优先级

当前最紧要目标是打通人工处理闭环：

客户提交问题 → Agent 判断 → 自动回复或建工单 → 客服坐席看到待处理工单 → 坐席回复或更新状态 → 客户侧看到最新进度。

优先实现客服坐席端最小闭环，再扩展完整角色权限、主管统计、多智能体或流式输出。

## 构建、测试与本地开发命令

启动 Java 业务服务：

```bash
cd business-service
mvn spring-boot:run
```

启动 Agent API：

```bash
cd ai-agent-service
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

启动 Streamlit 客户入口：

```bash
cd frontend-demo
streamlit run streamlit_app.py
```

Python 语法检查：

```bash
cd ai-agent-service
python -m compileall agents graphs rag schemas tools repositories app.py
```

## 编码风格与命名规范

所有文件统一使用 UTF-8。Python 使用 4 空格缩进，优先补充类型提示；函数和模块使用 `snake_case`，类和 Pydantic 模型使用 `PascalCase`。Java 遵循 Spring 常见约定：类名使用 `PascalCase`，方法和字段使用 `camelCase`，包路径统一放在 `com.example.business` 下。

Agent 流程职责要清晰：LangGraph 工作流节点放在 `graphs/`，LangChain 链路放在 `agents/` 或 `rag/`，外部业务系统调用放在 `tools/`。

## 代码注释规范

编写、修改、重构代码时，必须为关键代码添加中文注释。新增代码同步补充注释；修改业务逻辑时同步更新旧注释；不允许生成英文注释。

Java 使用 Javadoc 风格注释。核心类必须说明业务作用；Controller、Service、Mapper、Agent、Tool、State、Node 等结构需要说明其职责。核心方法必须说明方法作用；复杂方法需要说明入参、返回值和异常情况。

Python 使用中文 docstring。核心类、核心函数、Agent 节点、工具调用、RAG 链路和状态流转函数都需要说明业务职责。

关键条件判断、状态流转、权限校验、数据库查询、Agent 节点流转、工具调用等逻辑，需要添加简洁中文行内注释。注释要解释业务职责或设计原因，不要只写“处理数据”“执行逻辑”等空泛描述。

## 测试规范

当前仓库尚未建立完整测试套件。新增 Python 测试请放在 `ai-agent-service/tests/`，建议使用 `pytest`，文件命名示例：`test_customer_service_agent.py`。Java 测试请放在 `business-service/src/test/java`。

测试重点应覆盖登录鉴权、会话隔离、意图识别、风险路由、RAG 引用、工具调用失败、订单查询、工单创建和 Agent 状态流转。

## 提交与 Pull Request 规范

建议使用简洁的约定式提交信息：

```text
feat: add staff ticket console
fix: protect customer session access
docs: update contributor guide
```

Pull Request 需要包含变更摘要、影响模块、本地运行过的命令，以及与 PRD 或需求的对应关系。涉及 Streamlit 页面变化时，请附截图或简短说明。涉及业务规则、Agent 流程或风险控制的改动，需要说明验证方式。

## 安全与配置建议

不要提交 API Key、模型凭证、客户隐私数据或生成的虚拟环境。模型配置、RAG 配置和业务服务地址应通过环境变量维护。

Agent 工具不得直接执行退款、赔付、删除、关闭投诉等高风险动作。高风险动作必须进入人工审核或 Java 业务系统权限控制。
