param(
    [switch]$SkipDocker,
    [switch]$SkipJava,
    [switch]$SkipAgent,
    [switch]$SkipFrontend,
    [int]$DockerCommandTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RedisUrl = "redis://127.0.0.1:6379/0"
# 本地一键启动时显式指定 Java 服务地址，避免继承到其他环境的 BUSINESS_SERVICE_URL。
$BusinessServiceUrl = "http://127.0.0.1:8081"
$agentDir = Join-Path $Root "ai-agent-service"
$python = Join-Path $agentDir ".venv\Scripts\python.exe"
$runtimeDir = Join-Path $Root ".runtime"
$javaPidFile = Join-Path $runtimeDir "business-service.json"
$javaLogFile = Join-Path $runtimeDir "business-service.log"
$javaErrorLogFile = Join-Path $runtimeDir "business-service-error.log"
$agentLogFile = Join-Path $runtimeDir "agent-api.log"
$agentWorkerLogFile = Join-Path $runtimeDir "agent-worker-v2.log"
$followupWorkerLogFile = Join-Path $runtimeDir "followup-worker-v2.log"
$summaryWorkerLogFile = Join-Path $runtimeDir "conversation-summary-worker-v1.log"
$frontendLogFile = Join-Path $runtimeDir "frontend-vue.log"

function Test-PortOpen {
    param([int]$Port)
    if ($script:python -and (Test-Path $script:python)) {
        try {
            # Get-NetTCPConnection 在普通 Windows 用户下可能无权读取，改用实际 TCP 连接判断。
            & $script:python -c "import socket, sys; connection = socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=0.8); connection.close()" $Port 2>$null
            return $LASTEXITCODE -eq 0
        } catch {
            return $false
        }
    }
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-PortOpen {
    param([int]$Port, [int]$TimeoutSeconds = 30, [string]$Name = "service")
    for ($attempt = 0; $attempt -lt $TimeoutSeconds; $attempt++) {
        if (Test-PortOpen $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    Write-Warning "$Name did not listen on port $Port within $TimeoutSeconds seconds."
    return $false
}

function Test-TcpEndpoint {
    param([string]$HostName, [int]$Port, [int]$TimeoutMilliseconds = 800)
    $client = [System.Net.Sockets.TcpClient]::new()
    $asyncResult = $null
    try {
        # Windows PowerShell 5.1 对 ConnectAsync().Wait() 的返回处理偶尔会误判，
        # 使用 BeginConnect + WaitOne 明确等待连接结果，避免 Redis 可用时跳过 Worker。
        $asyncResult = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $client.Connected
    } catch {
        return $false
    } finally {
        if ($asyncResult -and $asyncResult.AsyncWaitHandle) {
            $asyncResult.AsyncWaitHandle.Close()
        }
        $client.Dispose()
    }
}

function Test-RedisReady {
    param([string]$PythonPath, [string]$Url)
    if (-not (Test-Path $PythonPath)) {
        return Test-TcpEndpoint "127.0.0.1" 6379
    }
    try {
        # 使用 Worker 相同的 Python Redis 客户端验证 PONG，避免端口探测误判。
        # 显式配置连接和读取超时：端口存在但 Redis 卡死时，启动脚本不能无限停在首行。
        & $PythonPath -c "import redis, sys; client=redis.Redis.from_url(sys.argv[1], socket_connect_timeout=1, socket_timeout=2); raise SystemExit(0 if client.ping() else 1)" $Url 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Wait-RedisReady {
    param([string]$PythonPath, [string]$Url, [int]$TimeoutSeconds = 30)
    for ($attempt = 0; $attempt -lt $TimeoutSeconds; $attempt++) {
        if (Test-RedisReady $PythonPath $Url) { return $true }
        Start-Sleep -Seconds 1
    }
    Write-Warning "Redis did not return PONG within $TimeoutSeconds seconds."
    return $false
}

function Wait-TcpEndpoint {
    param([string]$HostName, [int]$Port, [int]$TimeoutSeconds = 30, [string]$Name = "service")
    for ($attempt = 0; $attempt -lt $TimeoutSeconds; $attempt++) {
        if (Test-TcpEndpoint $HostName $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    Write-Warning "$Name did not accept connections on ${HostName}:$Port within $TimeoutSeconds seconds."
    return $false
}

function Start-ServiceWindow {
    param([string]$Title, [string]$WorkingDirectory, [string]$Command, [string]$LogFile = "")
    # 使用 Transcript 记录子窗口输出。不能用 `*>&1 | Tee-Object`：Uvicorn 会把正常 INFO
    # 写入 stderr，PowerShell 会将其格式化成 NativeCommandError，造成“启动报错”的误解。
    if ($LogFile) {
        $script = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location '$WorkingDirectory'; Start-Transcript -Path '$LogFile' -Append -Force | Out-Null; try { $Command } finally { Stop-Transcript | Out-Null }"
    } else {
        $script = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location '$WorkingDirectory'; $Command"
    }
    Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $script -WorkingDirectory $WorkingDirectory -WindowStyle Normal
}

function Test-AgentHealthy {
    # 端口监听并不代表 Uvicorn 已完成 RAG、Repository 等启动检查，必须探测健康端点。
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-AgentHealthy {
    param([int]$TimeoutSeconds = 120)
    for ($attempt = 0; $attempt -lt $TimeoutSeconds; $attempt++) {
        if (Test-AgentHealthy) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Test-JavaHealthy {
    # 通过 Actuator 健康接口确认业务服务真实可用，避免仅凭端口监听误判。
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8081/actuator/health" -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-ManagedJavaProcess {
    # 读取脚本创建的 Java 服务 PID；启动时间不一致时拒绝接管，避免 PID 复用误杀其他进程。
    if (-not (Test-Path $javaPidFile)) {
        return $null
    }
    try {
        $metadata = Get-Content -Raw -Path $javaPidFile | ConvertFrom-Json
        $process = Get-Process -Id ([int]$metadata.pid) -ErrorAction Stop
        $recordedStartedAt = ([datetime]$metadata.started_at).ToUniversalTime()
        $actualStartedAt = $process.StartTime.ToUniversalTime()
        if ([math]::Abs(($actualStartedAt - $recordedStartedAt).TotalSeconds) -gt 1) {
            Write-Warning "business-service PID 文件与当前进程启动时间不一致，已拒绝接管该进程。"
            return $null
        }
        return [pscustomobject]@{ Process = $process; Metadata = $metadata }
    } catch {
        Remove-Item -Path $javaPidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
}

function Stop-ManagedJavaProcess {
    # 停止由启动脚本创建的 Maven/Java 进程树，不处理未知的 8081 占用进程。
    $managed = Get-ManagedJavaProcess
    if (-not $managed) {
        return $false
    }
    Write-Host "Stopping managed Java business-service (PID $($managed.Process.Id))..."
    & taskkill.exe /PID $managed.Process.Id /T /F | Out-Null
    Remove-Item -Path $javaPidFile -Force -ErrorAction SilentlyContinue
    return $true
}

function Wait-JavaHealthy {
    # 等待 Spring Boot 完成 Maven 编译、数据库初始化与 Actuator 就绪。
    param([int]$TimeoutSeconds = 90)
    for ($attempt = 0; $attempt -lt $TimeoutSeconds; $attempt++) {
        if (Test-JavaHealthy) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Start-ManagedJavaService {
    # 启动并登记 Java 服务进程；失败时保留日志，供排查 Maven 或 Spring Boot 初始化错误。
    if (Test-JavaHealthy) {
        Write-Host "Java business-service is healthy on port 8081."
        return $true
    }
    if (Test-PortOpen 8081) {
        Write-Warning "Port 8081 is occupied but /actuator/health is unavailable. Refusing to replace an unmanaged process."
        return $false
    }
    if (Get-ManagedJavaProcess) {
        Stop-ManagedJavaProcess | Out-Null
    }

    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    Remove-Item -Path $javaPidFile, $javaLogFile, $javaErrorLogFile -Force -ErrorAction SilentlyContinue
    Write-Host "Starting managed Java business-service on port 8081..."
    # 以 cmd /c 持有 Maven 父进程，关闭时使用 taskkill /T 能同时结束 Maven 与其 Java 子进程。
    $process = Start-Process -FilePath "cmd.exe" -ArgumentList @("/d", "/c", "mvn spring-boot:run") `
        -WorkingDirectory (Join-Path $Root "business-service") -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $javaLogFile -RedirectStandardError $javaErrorLogFile
    $metadata = [pscustomobject]@{
        pid = $process.Id
        started_at = $process.StartTime.ToUniversalTime().ToString("o")
        command = "mvn spring-boot:run"
        working_directory = (Join-Path $Root "business-service")
    }
    $metadata | ConvertTo-Json | Set-Content -Path $javaPidFile -Encoding UTF8

    if (Wait-JavaHealthy 90) {
        Write-Host "Java business-service is healthy (PID $($process.Id))."
        return $true
    }

    Write-Warning "Java business-service did not become healthy within 90 seconds."
    if (Test-Path $javaErrorLogFile) {
        Write-Warning "Last Java error log lines:"
        Get-Content -Path $javaErrorLogFile -Tail 30 -ErrorAction SilentlyContinue | ForEach-Object { Write-Warning $_ }
    }
    return $false
}

function Invoke-Docker {
    param(
        [string[]]$Arguments,
        [int]$TimeoutSeconds = $DockerCommandTimeoutSeconds,
        [switch]$Silent
    )
    $job = $null
    try {
        $display = "docker " + ($Arguments -join " ")
        if (-not $Silent) { Write-Host "Running: $display" }
        $job = Start-Job -ScriptBlock {
            param([string[]]$ArgsList)
            & docker @ArgsList
            $exitCode = $LASTEXITCODE
            if ($exitCode -ne 0) {
                throw "docker failed with exit code $exitCode"
            }
        } -ArgumentList (,$Arguments)
        if (-not (Wait-Job -Job $job -Timeout $TimeoutSeconds)) {
            Stop-Job -Job $job -ErrorAction SilentlyContinue
            throw "$display timed out after $TimeoutSeconds seconds."
        }
        $result = Receive-Job -Job $job 2>&1
        if ($job.State -eq "Failed") {
            throw "$display failed. $($job.ChildJobs[0].JobStateInfo.Reason.Message)"
        }
        $text = ($result | ForEach-Object { $_.ToString() }) -join "`n"
        if ($text -and -not $Silent) {
            Write-Host $text.Trim()
        }
        return ($text.Trim())
    } finally {
        if ($job) {
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
    }
}

function Convert-Lines {
    param([string]$Text)
    if (-not $Text) { return @() }
    return @($Text -split "\r?\n" | Where-Object { $_ })
}

function Initialize-DockerConfig {
    if (-not $env:DOCKER_CONFIG) {
        $dockerConfig = Join-Path $Root ".docker-runtime-config"
        New-Item -ItemType Directory -Path $dockerConfig -Force | Out-Null
        $env:DOCKER_CONFIG = $dockerConfig
        Write-Host "Using local Docker config: $dockerConfig"
    }
}

function Test-DockerReady {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Warning "Docker command is unavailable. Redis queue cannot start from Docker."
        return $false
    }
    try {
        Initialize-DockerConfig
        Write-Host "Checking Docker daemon..."
        Invoke-Docker -Arguments @("info", "--format", "{{.ServerVersion}}") -TimeoutSeconds 15 -Silent | Out-Null
        return $true
    } catch {
        Write-Warning "Docker is not ready: $($_.Exception.Message)"
        Write-Warning "Start Docker Desktop first and make sure the current Windows user can access Docker."
        Write-Warning "If you see docker_engine permission denied, add the user to the docker-users group, then sign out and sign in again."
        return $false
    }
}

function Test-AgentWorkerAlive {
    param([string]$AgentDirectory)
    Push-Location $AgentDirectory
    try {
        & .\.venv\Scripts\python.exe -c "from services.agent_execution_queue import AgentExecutionQueue; raise SystemExit(0 if AgentExecutionQueue().has_active_worker() else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } finally {
        Pop-Location
    }
}

function Test-FollowupWorkerAlive {
    param([string]$AgentDirectory)
    Push-Location $AgentDirectory
    try {
        & .\.venv\Scripts\python.exe -c "from services.scheduled_followup_service import ScheduledFollowupQueue; raise SystemExit(0 if ScheduledFollowupQueue().has_active_worker() else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } finally {
        Pop-Location
    }
}

function Test-ConversationSummaryWorkerAlive {
    param([string]$AgentDirectory)
    Push-Location $AgentDirectory
    try {
        & .\.venv\Scripts\python.exe -c "from services.conversation_summary_service import ConversationSummaryQueue; raise SystemExit(0 if ConversationSummaryQueue().has_active_worker() else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } finally {
        Pop-Location
    }
}

function Start-ContainerIfNeeded {
    param([string]$Name, [string]$Image, [string[]]$Arguments)
    Write-Host "Checking container $Name..."
    $existingNames = Convert-Lines (Invoke-Docker -Arguments @("ps", "-a", "--filter", "name=$Name", "--format", "{{.Names}}") -TimeoutSeconds 15 -Silent)
    if ($existingNames -contains $Name) {
        $runningNames = Convert-Lines (Invoke-Docker -Arguments @("ps", "--filter", "name=$Name", "--filter", "status=running", "--format", "{{.Names}}") -TimeoutSeconds 15 -Silent)
        if (-not ($runningNames -contains $Name)) {
            Write-Host "Starting existing container $Name..."
            Invoke-Docker -Arguments @("start", $Name) -TimeoutSeconds 60 | Out-Null
        } else {
            Write-Host "Container $Name is already running."
        }
        return
    }
    Write-Host "Creating container $Name from $Image. First pull may take a while..."
    Invoke-Docker -Arguments (@("run", "--name", $Name) + $Arguments + @("-d", $Image)) -TimeoutSeconds $DockerCommandTimeoutSeconds | Out-Null
}

Write-Host "== Enterprise Customer Agent startup =="

# 固定使用 IPv4，并通过真实 Redis PONG 判断队列依赖是否可用。
$redisReady = Test-RedisReady $python $RedisUrl
$pgReady = Test-PortOpen 5432
if (-not $SkipDocker) {
    if (Test-DockerReady) {
        try {
            Start-ContainerIfNeeded "customer-agent-redis" "redis:7-alpine" @("-p", "6379:6379")
            Start-ContainerIfNeeded "customer-agent-pgvector" "pgvector/pgvector:pg16" @("-p", "5432:5432", "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_PASSWORD=postgres", "-e", "POSTGRES_DB=customer_agent")
            $redisReady = Wait-RedisReady $python $RedisUrl 30
            $pgReady = Wait-PortOpen 5432 30 "pgvector/PostgreSQL"
            if ($redisReady -and $pgReady) {
                Write-Host "Redis and pgvector are ready."
            }
        } catch {
            Write-Warning "Docker dependency startup failed: $($_.Exception.Message)"
        }
    }
}

$javaReady = $SkipJava -or (Test-JavaHealthy)
if (-not $SkipJava -and -not $javaReady) {
    $javaReady = Start-ManagedJavaService
} elseif (-not $SkipJava) {
    Write-Host "Java business-service is already healthy on port 8081."
}

$agentReady = $SkipAgent -or (Test-AgentHealthy)
if (-not $SkipAgent) {
    if (-not (Test-Path $python)) {
        Write-Warning "Python virtual environment is missing: $python"
    } else {
        $queueEnabled = if ($redisReady) { "true" } else { "false" }
        # Docker/PostgreSQL 未启动时仍应让本地 Demo 使用 SQLite + 内存 RAG 正常启动，
        # 不能继承终端中的 pgvector/PostgreSQL 配置而在启动阶段长时间阻塞。
        $databaseProvider = if ($pgReady) { $env:DB_PROVIDER } else { "sqlite" }
        if (-not $databaseProvider) { $databaseProvider = "sqlite" }
        $ragStoreBackend = if ($pgReady) { $env:RAG_STORE_BACKEND } else { "memory" }
        if (-not $ragStoreBackend) { $ragStoreBackend = "memory" }
        if (-not $pgReady) {
            Write-Warning "PostgreSQL is unavailable. Agent will use SQLite + memory RAG for this local run."
        }
        if (-not $redisReady) {
            Write-Warning "Redis is not available on port 6379. Agent Worker will not be started; intelligent replies may degrade."
        }
        if (-not $agentReady) {
            if (Test-PortOpen 8000) {
                Write-Warning "Port 8000 is occupied but /health is unavailable. Refusing to replace an unmanaged process."
                $agentReady = $false
            } else {
                New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
                Remove-Item -Path $agentLogFile -Force -ErrorAction SilentlyContinue
                Write-Host "Starting AI Agent API on port 8000..."
                Start-ServiceWindow "ai-agent-service :8000" $agentDir "`$env:REDIS_URL='$RedisUrl'; `$env:BUSINESS_SERVICE_URL='$BusinessServiceUrl'; `$env:AGENT_EXECUTION_QUEUE_ENABLED='$queueEnabled'; `$env:DB_PROVIDER='$databaseProvider'; `$env:RAG_STORE_BACKEND='$ragStoreBackend'; .\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000" $agentLogFile
                # Agent 初始化包含 RAG、Repository 和模型配置加载，必须通过健康检查再启动前端。
                $agentReady = Wait-AgentHealthy 120
            }
            if (-not $agentReady) {
                Write-Warning "AI Agent API did not become healthy within 120 seconds. See $agentLogFile"
                Get-Content -Path $agentLogFile -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object { Write-Warning $_ }
            }
        } else {
            Write-Host "AI Agent API is already healthy on port 8000."
        }
        if ($redisReady) {
            $env:REDIS_URL = $RedisUrl
            $env:AGENT_EXECUTION_QUEUE_ENABLED = "true"
            $workerAlive = Test-AgentWorkerAlive $agentDir
            if (-not $workerAlive) {
                Write-Host "Starting Agent Worker..."
                Remove-Item -Path $agentWorkerLogFile -Force -ErrorAction SilentlyContinue
                Start-ServiceWindow "agent-worker" $agentDir "`$env:REDIS_URL='$RedisUrl'; `$env:BUSINESS_SERVICE_URL='$BusinessServiceUrl'; `$env:AGENT_EXECUTION_QUEUE_ENABLED='true'; `$env:AGENT_WORKER_NAME='local-agent-worker'; `$env:DB_PROVIDER='$databaseProvider'; `$env:RAG_STORE_BACKEND='$ragStoreBackend'; .\.venv\Scripts\python.exe -m rag.agent_execution_worker" $agentWorkerLogFile
                for ($attempt = 0; $attempt -lt 10 -and -not $workerAlive; $attempt++) {
                    Start-Sleep -Seconds 1
                    $workerAlive = Test-AgentWorkerAlive $agentDir
                }
                if (-not $workerAlive) {
                    Write-Warning "Agent Worker heartbeat was not detected within 10 seconds. See $agentWorkerLogFile"
                } else {
                    Write-Host "Agent Worker heartbeat is healthy."
                }
            } else {
                Write-Host "Agent Worker heartbeat is already healthy."
            }
            # 通过版本化 Redis 心跳判断复核 Worker，避免依赖受权限限制的系统进程枚举。
            $followupWorkerAlive = Test-FollowupWorkerAlive $agentDir
            if (-not $followupWorkerAlive) {
                Write-Host "Starting Scheduled Follow-up Worker..."
                Remove-Item -Path $followupWorkerLogFile -Force -ErrorAction SilentlyContinue
                Start-ServiceWindow "followup-worker" $agentDir "`$env:REDIS_URL='$RedisUrl'; `$env:BUSINESS_SERVICE_URL='$BusinessServiceUrl'; `$env:DB_PROVIDER='$databaseProvider'; .\.venv\Scripts\python.exe -m rag.scheduled_followup_worker" $followupWorkerLogFile
                for ($attempt = 0; $attempt -lt 10 -and -not $followupWorkerAlive; $attempt++) {
                    Start-Sleep -Seconds 1
                    $followupWorkerAlive = Test-FollowupWorkerAlive $agentDir
                }
                if (-not $followupWorkerAlive) {
                    Write-Warning "Scheduled Follow-up Worker heartbeat was not detected within 10 seconds. See $followupWorkerLogFile"
                }
            } else {
                Write-Host "Scheduled Follow-up Worker is already running."
            }
            # 会话摘要使用独立 Stream 和模型舱壁，不能占用在线 Agent Worker。
            $summaryWorkerAlive = Test-ConversationSummaryWorkerAlive $agentDir
            if (-not $summaryWorkerAlive) {
                Write-Host "Starting Conversation Summary Worker..."
                Remove-Item -Path $summaryWorkerLogFile -Force -ErrorAction SilentlyContinue
                Start-ServiceWindow "conversation-summary-worker" $agentDir "`$env:REDIS_URL='$RedisUrl'; `$env:DB_PROVIDER='$databaseProvider'; `$env:CONVERSATION_SUMMARY_ENABLED='true'; .\.venv\Scripts\python.exe -m rag.conversation_summary_worker" $summaryWorkerLogFile
                for ($attempt = 0; $attempt -lt 10 -and -not $summaryWorkerAlive; $attempt++) {
                    Start-Sleep -Seconds 1
                    $summaryWorkerAlive = Test-ConversationSummaryWorkerAlive $agentDir
                }
                if (-not $summaryWorkerAlive) {
                    Write-Warning "Conversation Summary Worker heartbeat was not detected within 10 seconds. See $summaryWorkerLogFile"
                }
            } else {
                Write-Host "Conversation Summary Worker is already running."
            }
        } else {
            Write-Warning "Skipped Agent Worker because Redis is unavailable."
        }
    }
}

if (-not $SkipFrontend -and -not (Test-PortOpen 5173)) {
    if ((-not $SkipJava -and -not $javaReady) -or (-not $SkipAgent -and -not $agentReady)) {
        # 默认一键启动不在后端失败时继续拉起前端，避免首屏持续出现 ECONNREFUSED。
        Write-Warning "Frontend was not started because Java or Agent API is unavailable. Check the corresponding service window first."
    } else {
        New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
        Remove-Item -Path $frontendLogFile -Force -ErrorAction SilentlyContinue
        Write-Host "Starting frontend-vue on port 5173..."
        Start-ServiceWindow "frontend-vue :5173" (Join-Path $Root "frontend-vue") "npm run dev" $frontendLogFile
        if (-not (Wait-PortOpen 5173 45 "frontend-vue")) {
            Write-Warning "frontend-vue did not become ready. See $frontendLogFile"
            Get-Content -Path $frontendLogFile -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object { Write-Warning $_ }
        }
    }
} elseif (-not $SkipFrontend) {
    Write-Host "frontend-vue is already listening on port 5173."
}

Write-Host "Frontend: http://localhost:5173"
Write-Host "Agent:    http://localhost:8000/health"
Write-Host "Java:     http://localhost:8081/actuator/health"
Write-Host "Redis:    $RedisUrl"

# 启动脚本必须把核心服务失败以非零退出码返回给 bat/终端，避免表面“启动完成”而前端持续 ECONNREFUSED。
$criticalFailure = (-not $SkipJava -and -not $javaReady) -or (-not $SkipAgent -and -not $agentReady)
if ($criticalFailure) {
    Write-Error "Startup failed: Java business-service or AI Agent API is unavailable. Check .runtime logs and the service window."
    exit 1
}
