package com.example.business.entity;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 订单实体，承载 Agent 回答物流、退款、换货等问题所需的订单状态信息。
 */
public record OrderInfo(
        Long id,
        String orderNo,
        Long customerId,
        Long productId,
        String orderStatus,
        LocalDateTime payTime,
        LocalDateTime shipTime,
        LocalDateTime signTime,
        BigDecimal amount,
        String afterSaleStatus
) {
}
