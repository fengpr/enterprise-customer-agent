package com.example.business.config;

import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.ApplicationListener;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;

/**
 * 为本地一键启动器提供独立的应用就绪标记。
 *
 * <p>部分 Windows 受限运行环境无法从启动器访问 loopback 健康接口，也无法读取
 * Java 正在占用的重定向日志。仅当显式配置 BUSINESS_STARTUP_READY_FILE 时写入标记，
 * 生产环境未配置该变量时不会产生额外文件或改变服务启动行为。</p>
 */
@Component
public class StartupReadinessMarker implements ApplicationListener<ApplicationReadyEvent> {

    private static final Logger LOGGER = LoggerFactory.getLogger(StartupReadinessMarker.class);
    private final Path markerPath;

    public StartupReadinessMarker() {
        String configuredPath = System.getenv("BUSINESS_STARTUP_READY_FILE");
        this.markerPath = configuredPath == null || configuredPath.isBlank()
                ? null
                : Path.of(configuredPath).toAbsolutePath().normalize();
    }

    /**
     * Spring 完成端口绑定、数据库初始化和 Bean 创建后写入本次进程的就绪信息。
     *
     * @param event Spring Boot 应用就绪事件
     */
    @Override
    public void onApplicationEvent(ApplicationReadyEvent event) {
        if (markerPath == null) {
            return;
        }
        try {
            Path parent = markerPath.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            String content = "pid=" + ProcessHandle.current().pid() + System.lineSeparator()
                    + "ready_at=" + Instant.now() + System.lineSeparator();
            Files.writeString(markerPath, content, StandardCharsets.UTF_8);
        } catch (IOException exception) {
            // 就绪标记只服务于本地启动管理，写入失败不能影响业务服务。
            LOGGER.warn("无法写入本地启动就绪标记：{}", markerPath, exception);
        }
    }

    /**
     * 正常关闭时删除标记，避免下次启动把旧文件误判为当前服务已就绪。
     */
    @PreDestroy
    public void removeMarker() {
        if (markerPath == null) {
            return;
        }
        try {
            Files.deleteIfExists(markerPath);
        } catch (IOException exception) {
            LOGGER.warn("无法删除本地启动就绪标记：{}", markerPath, exception);
        }
    }
}
