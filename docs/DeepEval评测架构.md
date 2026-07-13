# DeepEval 在线评测架构

## 运行边界

客户请求只采集脱敏 Trace，不调用 DeepEval。线上评测和黄金集回归由独立 Worker 执行，避免 Judge 调用影响客户响应时间。

```text
客户请求 -> evaluation_trace(SQLite) -> evaluation_worker -> DeepEval -> evaluation_result(SQLite)
黄金集任务 -> evaluation_job(SQLite) -> evaluation_worker -> DeepEval -> 任务报告
```

## 启动 Worker

```powershell
cd ai-agent-service
.\.venv\Scripts\python.exe -m rag.evaluation_worker --interval 60
```

单次处理一个批次：

```powershell
.\.venv\Scripts\python.exe -m rag.evaluation_worker --once
```

Agent 服务默认也会启动内嵌 Dispatcher，创建黄金集任务后约 2 秒内自动领取；独立 Worker 可与其并行部署，SQLite 原子领取会避免重复执行。

## 默认策略

- 高风险、人工转接、工具失败、低置信度、无知识库命中和引用异常：100% 采样。
- 普通成功会话：稳定 10% 采样。
- 每日 Judge 预算：`EVAL_DAILY_BUDGET=200`。
- 单次 Worker 批量：10 条；单进程串行评测以降低供应商限流风险。
- 黄金集样本并发：`RAG_GOLDEN_EVAL_CONCURRENCY=3`，例如 5 条样本会按 3 条和 2 条两批并行执行。
- Trace 脱敏后保存；订单号、手机号和邮箱不会进入评测库。
