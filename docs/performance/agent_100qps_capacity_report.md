# Agent 分阶段容量报告

## 当前状态

尚未执行 10 QPS 实测。本机 Agent API（`localhost:8000`）和 Java 业务服务（`localhost:8081`）健康检查均超时，且未检测到 k6/Locust 可执行程序。因此不能生成真实 P50/P95/P99、队列、LLM 或数据库容量结论。

## 已准备压测脚本

脚本：`load-tests/agent-reply.js`。它覆盖政策咨询、订单物流、工单、动作请求、高风险、越界问题、SSE 与幂等回放；默认只运行 10 QPS、10 分钟。

执行前必须提供已授权的测试客户 Token，并分别启动 Agent API、Java 服务、Redis、PostgreSQL 与至少一个 Agent Worker：

```powershell
k6 run -e AGENT_BASE_URL=http://localhost:8000 -e CUSTOMER_TOKEN=<测试Token> -e TARGET_QPS=10 -e DURATION=10m load-tests/agent-reply.js
```

执行期间抓取 `/metrics`、Java `/actuator/prometheus` 和 OTLP Trace。只有 10 QPS 结束后队列可回落、DLQ 为零、5xx 低于 1%、API P95 不高于 1 秒时，才可继续 20 QPS。
