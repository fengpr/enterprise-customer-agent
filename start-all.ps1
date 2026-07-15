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
$agentDir = Join-Path $Root "ai-agent-service"
$python = Join-Path $agentDir ".venv\Scripts\python.exe"

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
        & $PythonPath -c "import redis, sys; raise SystemExit(0 if redis.Redis.from_url(sys.argv[1]).ping() else 1)" $Url 2>$null
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
    param([string]$Title, [string]$WorkingDirectory, [string]$Command)
    $script = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location '$WorkingDirectory'; $Command"
    Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $script -WorkingDirectory $WorkingDirectory -WindowStyle Normal
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

$javaReady = $SkipJava -or (Test-PortOpen 8081)
if (-not $SkipJava -and -not $javaReady) {
    Write-Host "Starting Java business-service on port 8081..."
    Start-ServiceWindow "business-service :8081" (Join-Path $Root "business-service") "mvn spring-boot:run"
    # Java 首次编译和 SQLite 初始化可能需要数秒；前端启动前必须等待业务接口可连接。
    $javaReady = Wait-PortOpen 8081 60 "Java business-service"
} elseif (-not $SkipJava) {
    Write-Host "Java business-service is already listening on port 8081."
}

$agentReady = $SkipAgent -or (Test-PortOpen 8000)
if (-not $SkipAgent) {
    if (-not (Test-Path $python)) {
        Write-Warning "Python virtual environment is missing: $python"
    } else {
        $queueEnabled = if ($redisReady) { "true" } else { "false" }
        if (-not $redisReady) {
            Write-Warning "Redis is not available on port 6379. Agent Worker will not be started; intelligent replies may degrade."
        }
        if (-not $agentReady) {
            Write-Host "Starting AI Agent API on port 8000..."
            Start-ServiceWindow "ai-agent-service :8000" $agentDir "`$env:REDIS_URL='$RedisUrl'; `$env:AGENT_EXECUTION_QUEUE_ENABLED='$queueEnabled'; .\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000"
            # Agent 初始化包含 RAG、Repository 和模型配置加载，必须就绪后才能让 Vite 发起首屏请求。
            $agentReady = Wait-PortOpen 8000 120 "AI Agent API"
        } else {
            Write-Host "AI Agent API is already listening on port 8000."
        }
        if ($redisReady) {
            $env:REDIS_URL = $RedisUrl
            $env:AGENT_EXECUTION_QUEUE_ENABLED = "true"
            $workerAlive = Test-AgentWorkerAlive $agentDir
            if (-not $workerAlive) {
                Write-Host "Starting Agent Worker..."
                Start-ServiceWindow "agent-worker" $agentDir "`$env:REDIS_URL='$RedisUrl'; `$env:AGENT_EXECUTION_QUEUE_ENABLED='true'; `$env:AGENT_WORKER_NAME='local-agent-worker'; .\.venv\Scripts\python.exe -m rag.agent_execution_worker"
                for ($attempt = 0; $attempt -lt 10 -and -not $workerAlive; $attempt++) {
                    Start-Sleep -Seconds 1
                    $workerAlive = Test-AgentWorkerAlive $agentDir
                }
                if (-not $workerAlive) {
                    Write-Warning "Agent Worker heartbeat was not detected within 10 seconds. Check the agent-worker window."
                } else {
                    Write-Host "Agent Worker heartbeat is healthy."
                }
            } else {
                Write-Host "Agent Worker heartbeat is already healthy."
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
        Write-Host "Starting frontend-vue on port 5173..."
        Start-ServiceWindow "frontend-vue :5173" (Join-Path $Root "frontend-vue") "npm run dev"
    }
} elseif (-not $SkipFrontend) {
    Write-Host "frontend-vue is already listening on port 5173."
}

Write-Host "Frontend: http://localhost:5173"
Write-Host "Agent:    http://localhost:8000/health"
Write-Host "Java:     http://localhost:8081/actuator/health"
Write-Host "Redis:    $RedisUrl"
