package com.example.business.entity;

/**
 * 客户实体，表示客服场景中可被查询的用户基础资料。
 */
public record Customer(
        Long id,
        String customerName,
        String phoneMasked,
        String level,
        Integer status
) {
}
