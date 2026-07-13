# 可观测性接入

Python Agent 暴露 `GET /metrics`，Java 业务服务通过 Actuator 暴露 `GET /actuator/prometheus`。Prometheus 应分别抓取这两个地址，再导入 `grafana-dashboard.json` 和 `agent-alerts.yml`。

关联字段仅使用 `X-Request-ID`、`X-Trace-ID`。禁止在日志、Trace 属性、指标标签中加入 Authorization、手机号、邮箱、地址或原始订单号；业务标识必须使用哈希。

建议告警阈值：API P95 大于 2 秒、5xx 大于 2%、队列大于 100、死信队列非空、LLM 超时/429/熔断升高、降级率升高、数据库连接池接近上限、Redis 不可用或 Worker 指标停止增长。
