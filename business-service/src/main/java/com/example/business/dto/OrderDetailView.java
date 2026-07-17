package com.example.business.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/** 客户侧订单详情 DTO，仅包含当前客户可见的订单、支付、配送和收货信息。 */
public record OrderDetailView(
        Long id, String orderNo, Long customerId, Long productId, String productName, String productCategory, Integer quantity,
        Integer warrantyDays, Boolean returnable, String orderStatus, LocalDateTime payTime, LocalDateTime shipTime,
        LocalDateTime signTime, BigDecimal amount, String afterSaleStatus, String receiverName,
        String receiverPhoneMasked, String shippingAddress, String paymentMethod, String deliveryMethod,
        BigDecimal freightAmount
) {
}
