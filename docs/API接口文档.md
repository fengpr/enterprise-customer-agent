# API 接口文档

## 统一约定

- 数据格式：JSON。
- 后台接口正式环境必须鉴权，Demo 阶段可使用本地 mock。
- 敏感字段返回前必须脱敏。
- Agent 工具调用失败时不编造结果，应返回失败原因并转人工。

## AI Agent Service

### 健康检查

`GET /health`

返回：

```json
{"status": "ok"}
```

### 登录

`POST /api/auth/login`

Java `business-service` 负责真实登录认证。Agent 服务会代理该接口，前端也可以统一调用 `http://localhost:8000/api/auth/login`。

请求：

```json
{"username": "demo", "password": "123456"}
```

Demo 账号：

- `demo / 123456`：客户 ID 为 `1`。
- `buyer / 123456`：客户 ID 为 `2`。

返回：

```json
{
  "token": "jwt-token",
  "token_type": "Bearer",
  "user": {
    "user_id": 1,
    "customer_id": 1,
    "display_name": "Demo Customer",
    "role": "customer"
  }
}
```

### 当前登录用户

`GET /api/auth/current-user`

请求头：

```http
Authorization: Bearer <token>
```

返回当前 Token 对应的用户。Agent 不再使用环境变量模拟登录人员，也不信任前端伪造的 `customer_id`。

### 意图识别与结构化抽取

`POST /api/agent/analyze`

请求：

```json
{"message": "我的订单 EC202606220001 想退款"}
```

核心返回字段：

- `intent`：业务域，取值为咨询、物流、退款、换货、维修、投诉、发票、会员、其他。
- `user_goal`：用户真实目的，取值为规则咨询、状态查询、发起申请、投诉、争议、普通信息查询、其他。
- `emotion`：普通、焦急、不满、强烈投诉。
- `order_no`：订单号数组。
- `need_order_query`：是否需要查询订单。
- `need_ticket`：是否建议创建工单。
- `need_human`：是否需要转人工。
- `priority`：低、中、高、紧急。
- `confidence`：0 到 1。
- `action_type`：业务动作类型，例如 `return_goods`、`exchange_goods`、`repair_request`、`invoice_issue`。
- `action_slots`：业务动作已收集槽位，例如 `order_no`、`after_sale_reason`、`fault_description`、`invoice_title`。
- `missing_slots`：当前动作仍需补充的信息。
- `next_action`：下一步动作，例如 `collect_slots`、`create_ticket`、`cancel_pending`。

`intent` 不再直接等同于风险级别。例如 `intent=refund` 且 `user_goal=policy_consult` 表示退款规则或到账时效咨询，可在有知识库依据时自动回复；`user_goal=action_request`、`complaint` 或 `dispute` 才进入人工审核或工单闭环。

真实业务动作使用通用闭环处理：`intent + user_goal + action_type + action_slots + missing_slots + pending_action_request + next_action`。例如“我要退货”会识别为 `intent=refund`、`user_goal=action_request`、`action_type=return_goods`，缺订单或原因时只追问槽位；槽位齐全且订单归属校验通过后才创建工单。

### 生成客服回复

`POST /api/agent/reply`

请求：

```json
{
  "message": "退款一般多久到账？",
  "session_id": "S202606220001",
  "selected_order_no": "EC202606220001",
  "customer_id": 1
}
```

`customer_id` 由 Java Token 解析结果自动补齐。前端请求 Agent 时必须携带 `Authorization` 请求头；即使请求体传入 `customer_id`，后端也会以 Token 中的身份为准。续接历史会话时，`session_id` 必须属于当前登录客户，否则返回 403。`selected_order_no` 是客户侧当前选中的订单上下文，Agent 会通过 Java 订单接口再次按 Token 校验订单归属，校验通过后才用于生成贴合当前订单的回复。

当业务动作信息未补齐时，返回结果会在 `pending_action_request` 中保存当前动作状态。前端无需展示该 JSON；下一轮同会话请求会由 Agent 服务自动读取最近未完成 pending 并合并用户补充信息。

核心返回字段：

- `answer`：候选回复。
- `auto_send`：是否允许自动发送。
- `need_human`：是否转人工。
- `analysis`：结构化识别结果。
- `citations`：知识库引用来源。
- `tool_results`：订单、工单等工具调用结果。
- `ticket_result`：Java 业务服务返回的工单创建结果。
- `risk_reasons`：风险原因。

### 客户会话管理

`GET /api/chat/session/list`

查询当前登录客户自己的会话列表，不返回已删除会话。

`POST /api/chat/session`

显式创建一个新会话：

```json
{"title": "新会话"}
```

`GET /api/chat/session/{sessionId}`

查询当前客户自己的会话详情和消息列表。

`DELETE /api/chat/session/{sessionId}`

软删除当前客户自己的会话。删除后客户侧列表不再展示，但数据库保留会话和消息，用于工单追踪、坐席回复和审计。

### Agent 工具调用

`POST /api/agent/tool/call`

请求：

```json
{"tool_name": "query_order", "arguments": {"order_no": "EC202606220001"}}
```

## Business Service

### 查询订单

`GET /api/orders/{orderNo}`

用于 Agent 查询订单状态、支付时间、发货时间、签收时间和售后状态。必须携带 `Authorization` 请求头；服务端会校验订单是否属于当前登录客户。

### 查询客户订单列表

`GET /api/orders?customerId=1`

用于支持“查询我的订单”这类没有明确订单号的场景。必须携带 `Authorization` 请求头；`customerId` 参数仅兼容旧调用，实际查询以 Token 中的客户 ID 为准。

### 查询订单物流

`GET /api/orders/{orderNo}/logistics`

用于 Agent 查询订单关联的完整物流路线、转运站和轨迹节点。必须携带 `Authorization` 请求头；服务端会先校验订单是否属于当前登录客户，再返回物流信息。

返回示例：

```json
{
  "orderNo": "EC202606220002",
  "carrierName": "顺丰速运",
  "trackingNo": "SF202606230002",
  "logisticsStatus": "IN_TRANSIT",
  "latestLocation": "上海转运中心",
  "estimatedDeliveryTime": "2026-06-25T18:00:00",
  "routeSummary": "杭州仓库 -> 杭州集散中心 -> 上海转运中心",
  "traces": [
    {
      "status": "SHIPPED",
      "description": "商家已发货，快件已完成揽收。",
      "location": "杭州",
      "stationName": "杭州仓库",
      "occurredAt": "2026-06-23T10:00:00"
    }
  ]
}
```

### 查询客户

`GET /api/customers/{id}`

返回脱敏后的客户信息。

### 查询商品

`GET /api/products`

返回 Demo 商品列表。

## 工单接口

### 工单列表

`GET /api/tickets`

必须携带 `Authorization` 请求头，只返回当前登录客户自己的工单列表。

### 创建工单

`POST /api/tickets`

必须携带 `Authorization` 请求头。请求字段参考 `support_ticket` 表，但 `customerId` 以 Token 中的客户 ID 为准，不信任请求体。服务端生成 `ticket_no`、默认状态 `PENDING_ASSIGN` 和 SLA 截止时间。

### 工单详情

`GET /api/tickets/{ticketNo}`

按工单号查询当前登录客户自己的工单；访问他人工单返回业务错误。

### 催办工单

`POST /api/tickets/{ticketNo}/urge`

必须携带 `Authorization` 请求头，只允许催办当前登录客户自己的未关闭工单。请求：

```json
{"reason": "客户希望尽快处理退货申请"}
```

服务端会写入 `ticket_urge_log` 催办日志，并更新工单上的 `urgeCount`、`lastUrgedAt`、`lastUrgeReason`。返回更新后的工单详情，调度台和坐席台可看到催办次数与最近催办原因。

### 已废弃工单写接口

以下旧接口已收口，不再允许直接写工单状态：

- `POST /api/tickets/{ticketNo}/assign`
- `POST /api/tickets/{ticketNo}/transfer`
- `POST /api/tickets/{ticketNo}/status`
- `POST /api/tickets/{ticketNo}/close`
- `POST /api/tickets/{ticketNo}/reopen`

这些接口统一返回 HTTP 410：

```json
{
  "status": "failed",
  "error_message": "该工单写接口已废弃，请使用 /api/staff/tickets 或 /api/internal/tickets"
}
```

后续工单写动作必须走受控入口：

- 坐席领取、处理、关闭：`/api/staff/tickets/**`
- 调度分派、转派、智能派单：`/api/staff/tickets/**`
- Agent 建单后自动派单：`/api/internal/tickets/**`

### 错误响应

非法状态、工单不存在等业务错误返回 HTTP 400：

```json
{"status": "failed", "error_message": "当前状态不允许关闭：PENDING_ASSIGN"}
```
