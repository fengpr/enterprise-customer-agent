param(
    [switch]$SkipDocker,
    [switch]$SkipJava,
    [switch]$SkipAgent,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-PortOpen {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Start-ServiceWindow {
    param([string]$Title, [string]$WorkingDirectory, [string]$Command)
    $script = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location '$WorkingDirectory'; $Command"
    Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $script -WorkingDirectory $WorkingDirectory -WindowStyle Normal
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
    $existing = docker ps -a --filter "name=$Name" --format "{{.Names}}" 2>$null
    if ($existing -contains $Name) {
        $running = docker ps --filter "name=$Name" --filter "status=running" --format "{{.Names}}" 2>$null
        if (-not ($running -contains $Name)) { docker start $Name | Out-Null }
        return
    }
    docker run --name $Name @Arguments -d $Image | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker container $Name failed to start."
    }
}

Write-Host "== Enterprise Customer Agent startup =="

if (-not $SkipDocker) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Warning "Docker is unavailable. Redis queue cannot start."
    } else {
        # redis:7-alpine 默认命令就是 redis-server；命令参数必须位于镜像名之后，避免被 Docker 当作镜像。
        Start-ContainerIfNeeded "customer-agent-redis" "redis:7-alpine" @("-p", "6379:6379")
        Start-ContainerIfNeeded "customer-agent-pgvector" "pgvector/pgvector:pg16" @("-p", "5432:5432", "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_PASSWORD=postgres", "-e", "POSTGRES_DB=customer_agent")
        Write-Host "Redis and pgvector are ready or starting."
    }
}

if (-not $SkipJava -and -not (Test-PortOpen 8081)) {
    Start-ServiceWindow "business-service :8081" (Join-Path $Root "business-service") "mvn spring-boot:run"
}

$agentDir = Join-Path $Root "ai-agent-service"
$python = Join-Path $agentDir ".venv\Scripts\python.exe"
if (-not $SkipAgent) {
    if (-not (Test-Path $python)) {
        Write-Warning "Python virtual environment is missing: $python"
    } else {
        if (-not (Test-PortOpen 8000)) {
            Start-ServiceWindow "ai-agent-service :8000" $agentDir "`$env:REDIS_URL='redis://localhost:6379/0'; `$env:AGENT_EXECUTION_QUEUE_ENABLED='true'; .\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000"
        }
        # 不依赖 Win32_Process：权限受限时会漏查，且卡在模型调用中的 Worker 进程仍存在却无法消费任务。
        # 通过 Redis 心跳判断真实可工作的消费者，心跳过期后可安全启动替代 Worker 恢复 Pending 任务。
        $env:REDIS_URL = "redis://localhost:6379/0"
        $env:AGENT_EXECUTION_QUEUE_ENABLED = "true"
        $workerAlive = Test-AgentWorkerAlive $agentDir
        if (-not $workerAlive) {
            Start-ServiceWindow "agent-worker" $agentDir "`$env:REDIS_URL='redis://localhost:6379/0'; `$env:AGENT_EXECUTION_QUEUE_ENABLED='true'; .\.venv\Scripts\python.exe -m rag.agent_execution_worker"
            # 等待 Worker 写入首个 Redis 心跳，启动失败时在主窗口给出明确提示。
            for ($attempt = 0; $attempt -lt 10 -and -not $workerAlive; $attempt++) {
                Start-Sleep -Seconds 1
                $workerAlive = Test-AgentWorkerAlive $agentDir
            }
            if (-not $workerAlive) {
                Write-Warning "Agent Worker heartbeat was not detected within 10 seconds. Check the agent-worker window."
            } else {
                Write-Host "Agent Worker heartbeat is healthy."
            }
        }
    }
}

if (-not $SkipFrontend -and -not (Test-PortOpen 5173)) {
    Start-ServiceWindow "frontend-vue :5173" (Join-Path $Root "frontend-vue") "npm run dev"
}

Write-Host "Frontend: http://localhost:5173"
Write-Host "Agent:    http://localhost:8000/health"
Write-Host "Java:     http://localhost:8081/actuator/health"
Write-Host "Redis:    redis://localhost:6379/0"
