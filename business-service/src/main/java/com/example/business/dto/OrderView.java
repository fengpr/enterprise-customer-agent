package com.example.business.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 客户侧订单展示 DTO，将订单主数据和商品基础信息合并返回给前端与 Agent。
 */
public record OrderView(
        Long id,
        String orderNo,
        Long customerId,
        Long productId,
        String productName,
        String productCategory,
        Integer quantity,
        Integer warrantyDays,
        Boolean returnable,
        String orderStatus,
        LocalDateTime payTime,
        LocalDateTime shipTime,
        LocalDateTime signTime,
        BigDecimal amount,
        String afterSaleStatus
) {
}
