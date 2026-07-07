package com.example.business.dto;

import java.time.LocalDateTime;

/**
 * 物流轨迹展示 DTO，描述订单在揽收、运输、转运和签收过程中的单个节点。
 */
public record LogisticsTraceView(
        String status,
        String description,
        String location,
        String stationName,
        LocalDateTime occurredAt
) {
}
