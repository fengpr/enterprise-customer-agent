package com.example.business.dto;

/**
 * 客户补充工单信息请求。退货原因可以追加到在途工单，取件履约信息是否可直接修改由服务端状态规则决定。
 */
public record TicketSupplementRequest(
        String content,
        String afterSaleReason,
        String returnMethod,
        String pickupTimeWindow
) {
}
