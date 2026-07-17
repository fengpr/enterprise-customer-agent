/** 客户首页原型专用展示数据，后续接入聚合接口时只需替换该数据层。 */
export interface PrototypeOrder { product: string; orderNo: string; amount: string; time: string; status: string; icon: string }
export interface PrototypeTicket { no: string; type: string; status: string; time: string }

export const customerHomePrototype = {
  user: { name: 'Demo Customer', level: '普通客户', points: '2,680', totalOrders: 18, totalTickets: 12 },
  entries: [
    { key: 'orders', title: '我的订单', description: '查看订单状态与物流进度', action: '查看全部订单', count: 18, tone: 'blue' },
    { key: 'tickets', title: '我的工单', description: '跟踪工单处理进度', action: '查看全部工单', count: 12, tone: 'green' },
    { key: 'service', title: '在线客服', description: '智能助手与人工客服', action: '立即发起咨询', count: undefined, tone: 'purple' },
    { key: 'help', title: '帮助中心', description: '常见问题与操作指南', action: '进入帮助中心', count: undefined, tone: 'orange' }
  ],
  metrics: [
    { label: '待处理工单', value: '5', note: '较昨日 ↓ 1', tone: 'blue' },
    { label: '处理中', value: '7', note: '较昨日 ↑ 2', tone: 'orange' },
    { label: '最近订单', value: '3', note: '近7天新增', tone: 'green' },
    { label: '消息提醒', value: '2', note: '未读消息', tone: 'purple' }
  ],
  orders: [
    { product: 'Smart Router AX3000', orderNo: 'EC202606220001', amount: '¥399', time: '2026-06-16 16:02', status: '已签收', icon: '▰' },
    { product: 'Mesh WiFi 6 组网套装', orderNo: 'EC202606150308', amount: '¥799', time: '2026-06-15 15:30', status: '运输中', icon: '▯' },
    { product: '智能摄像头 Pro', orderNo: 'EC202606100125', amount: '¥299', time: '2026-06-10 09:18', status: '已完成', icon: '●' }
  ] as PrototypeOrder[],
  tickets: [
    { no: 'T20260625095309625', type: '退货申请', status: '处理中', time: '06-29 14:35' },
    { no: 'T2026062200015900', type: '投诉反馈', status: '待分派', time: '06-29 11:20' },
    { no: 'T2026062100452300', type: '技术支持', status: '处理中', time: '06-28 16:55' },
    { no: 'T2026061800387700', type: '发票问题', status: '已完成', time: '06-27 10:14' },
    { no: 'T2026061600123400', type: '退货/退货', status: '已关闭', time: '06-25 09:41' }
  ] as PrototypeTicket[],
  reminders: [
    { title: '工单待处理', desc: '您有 5 个工单待处理', action: '去处理', tone: 'orange' },
    { title: '待评价工单', desc: '有 2 个已完成工单等待评价', action: '去评价', tone: 'blue' }
  ],
  todos: [
    { title: '补充申请材料', count: 3 }, { title: '回复客服消息', count: 2 }, { title: '评价已完成工单', count: 1 }, { title: '确认处理结果', count: 1 }
  ],
  activities: [
    { time: '06-29 14:35', text: '工单 T20260625095309625 状态更新为【处理中】', tone: 'green' },
    { time: '06-29 11:20', text: '工单 T2026062200015900 已分派给客服专员', tone: 'blue' },
    { time: '06-28 16:55', text: '工单 T2026062100452300 客户已回复', tone: 'blue' }
  ]
} as const
