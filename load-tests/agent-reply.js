import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// 单档压测脚本：通过 TARGET_QPS/DURATION 控制，禁止默认跳过 10 QPS 直接压满。
const targetQps = Number(__ENV.TARGET_QPS || 10);
const duration = __ENV.DURATION || '10m';
const baseUrl = __ENV.AGENT_BASE_URL || 'http://localhost:8000';
const token = __ENV.CUSTOMER_TOKEN || '';
const customerId = __ENV.CUSTOMER_ID || 'load-test-customer';

const sseAcceptedLatency = new Trend('agent_sse_accepted_latency_ms');
const sseCompleted = new Rate('agent_sse_completed_rate');
const sseDegraded = new Counter('agent_sse_degraded_total');
const duplicateIdempotency = new Counter('agent_idempotency_replays_total');

export const options = {
  scenarios: {
    customer_mix: {
      executor: 'constant-arrival-rate',
      rate: targetQps,
      timeUnit: '1s',
      duration,
      preAllocatedVUs: Math.max(20, targetQps * 3),
      maxVUs: Math.max(100, targetQps * 10),
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<1000'],
    agent_sse_accepted_latency_ms: ['p(95)<2000'],
  },
};

const cases = [
  // 40% 政策咨询
  { weight: 40, message: '退货规则是什么，退款大概多久到账？' },
  { weight: 40, message: '电子发票如何申请？' },
  // 25% 订单/物流查询
  { weight: 25, message: '请查询订单 EC202606220001 的物流进度', order: 'EC202606220001' },
  // 15% 工单查询/催办
  { weight: 15, message: '请查看工单 T202606220001 的处理进度', ticket: 'T202606220001' },
  // 10% 动作请求
  { weight: 10, message: '订单 EC202606220001 商品有质量问题，我要申请退货' },
  // 5% 高风险
  { weight: 5, message: '我要投诉你们，要求人工处理' },
  // 5% 越界/无命中
  { weight: 5, message: '请推荐一支稳赚不赔的股票' },
];

function chooseCase() {
  const total = cases.reduce((sum, item) => sum + item.weight, 0);
  let point = Math.random() * total;
  for (const item of cases) {
    point -= item.weight;
    if (point <= 0) return item;
  }
  return cases[0];
}

function headers(requestId, idempotencyKey) {
  return {
    'Content-Type': 'application/json',
    'X-Request-ID': requestId,
    'Idempotency-Key': idempotencyKey,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

export default function () {
  const scenario = chooseCase();
  const requestId = `load-${__VU}-${__ITER}-${Date.now()}`;
  // 每 20 个请求覆盖一次 SSE；其余走同步接口与后续状态查询。
  const stream = __ITER % 20 === 0;
  const idempotencyKey = `load-idem-${__VU}-${__ITER}`;
  const body = JSON.stringify({
    message: scenario.message,
    customer_id: customerId,
    session_id: `load-session-${__VU % 50}`,
    selected_order_no: scenario.order || null,
    selected_ticket_no: scenario.ticket || null,
  });
  const started = Date.now();
  const response = http.post(`${baseUrl}/api/agent/reply${stream ? '/stream' : ''}`, body, {
    headers: headers(requestId, idempotencyKey),
    tags: { endpoint: stream ? 'agent_reply_stream' : 'agent_reply' },
    timeout: '35s',
  });

  check(response, { '响应状态受控': (r) => [200, 202, 401, 429, 503].includes(r.status) });
  if (stream) {
    sseAcceptedLatency.add(Date.now() - started);
    sseCompleted.add(response.body.includes('event: completed'));
    if (response.body.includes('event: degraded') || response.body.includes('event: error')) sseDegraded.add(1);
  }

  // 幂等回放：同一 Key 只在低频请求执行，避免放大模型调用。
  if (__ITER % 100 === 0) {
    const replay = http.post(`${baseUrl}/api/agent/reply`, body, { headers: headers(`${requestId}-replay`, idempotencyKey), timeout: '10s' });
    if ([200, 202].includes(replay.status)) duplicateIdempotency.add(1);
  }
  sleep(0.01);
}
