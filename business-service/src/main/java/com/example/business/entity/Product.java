package com.example.business.entity;

/**
 * 商品实体，表示售后政策判断中需要引用的商品属性。
 */
public record Product(
        Long id,
        String productName,
        String category,
        Integer warrantyDays,
        Boolean returnable,
        Integer status
) {
}
