package com.example.business.config;

import org.apache.coyote.http11.Http11Nio2Protocol;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.web.embedded.tomcat.TomcatServletWebServerFactory;
import org.springframework.boot.web.server.WebServerFactoryCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 本地 Windows Tomcat 协议兼容配置。
 *
 * <p>部分安装了网络过滤驱动或运行在受限桌面容器中的 Windows 环境无法创建
 * Java NIO Selector 所需的内部 loopback 连接。开启配置后改用 NIO2/IOCP，
 * 绕过该 Selector 初始化路径；生产环境未显式开启时仍保持 Spring Boot 默认协议。</p>
 */
@Configuration
@ConditionalOnProperty(name = "business.tomcat.nio2-enabled", havingValue = "true")
public class TomcatProtocolConfiguration {

    /**
     * 将本地嵌入式 Tomcat 切换为 NIO2 协议。
     *
     * @return Tomcat WebServer 工厂定制器
     */
    @Bean
    public WebServerFactoryCustomizer<TomcatServletWebServerFactory> tomcatNio2Customizer() {
        return factory -> factory.setProtocol(Http11Nio2Protocol.class.getName());
    }
}
