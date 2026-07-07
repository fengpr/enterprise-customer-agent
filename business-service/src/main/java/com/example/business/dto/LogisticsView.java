package com.example.business.dto;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 物流详情展示 DTO，将物流主信息和完整轨迹合并返回给 Agent 和客户侧页面。
 */
public record LogisticsView(
        String orderNo,
        String carrierName,
        String trackingNo,
        String logisticsStatus,
        String latestLocation,
        LocalDateTime estimatedDeliveryTime,
        String routeSummary,
        List<LogisticsTraceView> traces
) {
}
