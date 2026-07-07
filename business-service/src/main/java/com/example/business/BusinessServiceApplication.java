package com.example.business;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 模拟业务系统启动类，承载订单、客户、商品和工单等存量业务接口。
 */
@SpringBootApplication
public class BusinessServiceApplication {
    /**
     * 启动 Spring Boot 应用，为 AI Agent 工具调用提供本地业务数据服务。
     *
     * @param args 启动参数
     */
    public static void main(String[] args) {
        SpringApplication.run(BusinessServiceApplication.class, args);
    }
}
