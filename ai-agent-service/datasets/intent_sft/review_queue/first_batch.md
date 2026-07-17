# 首批待审核候选样本

本批样本已按 `coverage_plan.md` 复核并转入正式 JSONL。本文件保留原始候选与复核结论，供学习标注审计过程；后续候选应继续先进入复核队列。

| 用户原话 | 建议 intent | 建议 user_goal | 已分配分片 |
| --- | --- | --- | --- |
| 退款的钱一般几天退回？ | refund | policy_consult | train |
| 我之前申请的退款还没有到账。 | refund | status_query | train |
| 订单 EC_TEST_0101 我不想要了，帮我退掉。 | refund | action_request | train |
| 退货入口在什么地方？ | refund | how_to | train |
| 快递显示签收但我没有拿到。 | logistics | status_query | train |
| 帮我催一下订单 EC_TEST_0102 的物流。 | logistics | status_query | validation |
| 商品型号不对，我想换货。 | exchange | action_request | train |
| 维修要怎么申请？ | repair | how_to | train |
| 订单 EC_TEST_0103 的维修处理进度如何？ | repair | status_query | validation |
| 税号已经有了，帮我给订单开专票。 | invoice | action_request | train |
| 商家说不退钱，我要维权。 | complaint | dispute | test |
| 你们会员到期后权益会怎样？ | member | policy_consult | test |
