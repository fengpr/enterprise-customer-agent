package com.example.business.controller;

import com.example.business.dto.LogisticsView;
import com.example.business.dto.OrderDetailView;
import com.example.business.dto.OrderView;
import com.example.business.service.AuthService;
import com.example.business.service.AgentExecutionCredentialService;
import com.example.business.service.LogisticsService;
import com.example.business.service.OrderService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Optional;

/**
 * 订单接口控制器，为 Agent 查询物流、退款、售后场景提供受控订单数据。
 */
@RestController
@RequestMapping("/api/orders")
public class OrderController {
    private final OrderService orderService;
    private final LogisticsService logisticsService;
    private final AuthService authService;
    private final AgentExecutionCredentialService executionCredentialService;

    public OrderController(OrderService orderService, LogisticsService logisticsService, AuthService authService,
                           AgentExecutionCredentialService executionCredentialService) {
        this.orderService = orderService;
        this.logisticsService = logisticsService;
        this.authService = authService;
        this.executionCredentialService = executionCredentialService;
    }

    /**
     * 根据客户 ID 查询订单列表，支持“查询我的订单”这类没有明确订单号的客服场景。
     *
     * @param customerId 前端传入的客户 ID，兼容旧调用；实际查询以 Token 中客户 ID 为准
     * @param authorization Authorization 请求头
     * @return 该客户关联的订单列表
     */
    @GetMapping
    public List<OrderView> listByCustomer(
            @RequestParam(value = "customerId", required = false) Long customerId,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestHeader(value = "X-Agent-Execution-Credential", required = false) String executionCredential,
            @RequestHeader(value = "X-Agent-Customer-ID", required = false) Long agentCustomerId,
            @RequestHeader(value = "X-Request-ID", required = false) String requestId
    ) {
        // 客户订单属于敏感业务数据，必须以登录 Token 中的客户 ID 为准。
        Long currentCustomerId = resolveCustomerId(authorization, executionCredential, agentCustomerId, requestId);
        return orderService.findViewsByCustomerId(currentCustomerId);
    }

    /**
     * 根据订单号查询订单详情。
     *
     * @param orderNo 用户提供或 Agent 抽取出的订单号
     * @param authorization Authorization 请求头
     * @return 可能存在的订单信息，未命中时返回空 Optional
     */
    @GetMapping("/{orderNo}")
    public Optional<OrderView> detail(
            @PathVariable("orderNo") String orderNo,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestHeader(value = "X-Agent-Execution-Credential", required = false) String executionCredential,
            @RequestHeader(value = "X-Agent-Customer-ID", required = false) Long agentCustomerId,
            @RequestHeader(value = "X-Request-ID", required = false) String requestId
    ) {
        Long currentCustomerId = resolveCustomerId(authorization, executionCredential, agentCustomerId, requestId);
        return orderService.findViewByOrderNo(orderNo)
                .filter(order -> order.customerId().equals(currentCustomerId));
    }

    /** 返回当前客户自己的订单详情，避免通过订单号读取他人的收货与支付信息。 */
    @GetMapping("/{orderNo}/detail")
    public Optional<OrderDetailView> detailForCustomer(
            @PathVariable("orderNo") String orderNo,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestHeader(value = "X-Agent-Execution-Credential", required = false) String executionCredential,
            @RequestHeader(value = "X-Agent-Customer-ID", required = false) Long agentCustomerId,
            @RequestHeader(value = "X-Request-ID", required = false) String requestId
    ) {
        Long currentCustomerId = resolveCustomerId(authorization, executionCredential, agentCustomerId, requestId);
        return orderService.findDetailViewByOrderNo(orderNo)
                .filter(order -> order.customerId().equals(currentCustomerId));
    }

    /**
     * 根据订单号查询完整物流轨迹。
     *
     * @param orderNo 用户提供或 Agent 抽取出的订单号
     * @param authorization Authorization 请求头
     * @return 物流主信息与完整轨迹，订单不属于当前客户或暂无物流时返回空 Optional
     */
    @GetMapping("/{orderNo}/logistics")
    public Optional<LogisticsView> logistics(
            @PathVariable("orderNo") String orderNo,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestHeader(value = "X-Agent-Execution-Credential", required = false) String executionCredential,
            @RequestHeader(value = "X-Agent-Customer-ID", required = false) Long agentCustomerId,
            @RequestHeader(value = "X-Request-ID", required = false) String requestId
    ) {
        Long currentCustomerId = resolveCustomerId(authorization, executionCredential, agentCustomerId, requestId);
        // 物流查询必须先校验订单归属，避免通过运单接口越权查看他人路线。
        return orderService.findViewByOrderNo(orderNo)
                .filter(order -> order.customerId().equals(currentCustomerId))
                .flatMap(order -> logisticsService.findByOrderNo(order.orderNo()));
    }

    /** 登录 Token 与 Worker 短期凭证二选一完成身份校验。 */
    private Long resolveCustomerId(String authorization, String executionCredential,
                                   Long agentCustomerId, String requestId) {
        if (authorization != null && !authorization.isBlank()) {
            return authService.currentCustomerId(authorization);
        }
        return executionCredentialService.requireCustomerId(executionCredential, agentCustomerId, requestId);
    }
}
