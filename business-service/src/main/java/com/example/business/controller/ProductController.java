package com.example.business.controller;

import com.example.business.entity.Product;
import com.example.business.service.ProductService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 商品接口控制器，为订单和售后判断提供商品基础信息。
 */
@RestController
@RequestMapping("/api/products")
public class ProductController {
    private final ProductService productService;

    public ProductController(ProductService productService) {
        this.productService = productService;
    }

    /**
     * 查询 Demo 商品列表。
     *
     * @return 当前模拟系统中的可用商品数据
     */
    @GetMapping
    public List<Product> list() {
        return productService.list();
    }
}
