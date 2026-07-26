package com.example.business.dto;

import com.example.business.entity.TicketChangeRequest;

import java.time.LocalDateTime;

/**
 * 客户侧可见的履约变更申请视图。
 * <p>刻意不暴露审核座席、幂等键和内部处理细节。</p>
 */
public record TicketChangeRequestCustomerView(
        String requestNo,
        String changeType,
        String requestedReturnMethod,
        String requestedPickupTimeWindow,
        String status,
        String customerMessage,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
    /** 将内部实体转换为客户安全字段。 */
    public static TicketChangeRequestCustomerView from(TicketChangeRequest request) {
        return new TicketChangeRequestCustomerView(
                request.requestNo(),
                request.changeType(),
                request.requestedReturnMethod(),
                request.requestedPickupTimeWindow(),
                request.status(),
                request.customerMessage(),
                request.createdAt(),
                request.updatedAt()
        );
    }
}
