package com.example.business.controller;

import com.example.business.entity.Customer;
import com.example.business.service.CustomerService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Optional;

/**
 * 客户接口控制器，为 Agent 和客服工作台提供脱敏客户信息查询能力。
 */
@RestController
@RequestMapping("/api/customers")
public class CustomerController {
    private final CustomerService customerService;

    public CustomerController(CustomerService customerService) {
        this.customerService = customerService;
    }

    /**
     * 根据客户 ID 查询客户详情。
     *
     * @param id 客户主键
     * @return 可能存在的客户信息，手机号等敏感字段由模拟数据保持脱敏
     */
    @GetMapping("/{id}")
    public Optional<Customer> detail(@PathVariable("id") Long id) {
        return customerService.findById(id);
    }
}
