param(
    [switch]$SkipDocker,
    [switch]$SkipJava,
    [switch]$SkipAgent,
    [switch]$SkipFrontend,
    [int]$DockerCommandTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-PortOpen {
    param([int]$Port)
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

$redisReady = Test-PortOpen 6379
if (-not $SkipDocker) {
    if (Test-DockerReady) {
        try {
            Start-ContainerIfNeeded "customer-agent-redis" "redis:7-alpine" @("-p", "6379:6379")
            Start-ContainerIfNeeded "customer-agent-pgvector" "pgvector/pgvector:pg16" @("-p", "5432:5432", "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_PASSWORD=postgres", "-e", "POSTGRES_DB=customer_agent")
            $redisReady = Wait-PortOpen 6379 30 "Redis"
            $pgReady = Wait-PortOpen 5432 30 "pgvector/PostgreSQL"
            if ($redisReady -and $pgReady) {
                Write-Host "Redis and pgvector are ready."
            }
        } catch {
            Write-Warning "Docker dependency startup failed: $($_.Exception.Message)"
        }
    }
}

if (-not $SkipJava -and -not (Test-PortOpen 8081)) {
    Write-Host "Starting Java business-service on port 8081..."
    Start-ServiceWindow "business-service :8081" (Join-Path $Root "business-service") "mvn spring-boot:run"
} elseif (-not $SkipJava) {
    Write-Host "Java business-service is already listening on port 8081."
}

$agentDir = Join-Path $Root "ai-agent-service"
$python = Join-Path $agentDir ".venv\Scripts\python.exe"
if (-not $SkipAgent) {
    if (-not (Test-Path $python)) {
        Write-Warning "Python virtual environment is missing: $python"
    } else {
        $queueEnabled = if ($redisReady) { "true" } else { "false" }
        if (-not $redisReady) {
            Write-Warning "Redis is not available on port 6379. Agent Worker will not be started; intelligent replies may degrade."
        }
        if (-not (Test-PortOpen 8000)) {
            Write-Host "Starting AI Agent API on port 8000..."
            Start-ServiceWindow "ai-agent-service :8000" $agentDir "`$env:REDIS_URL='redis://localhost:6379/0'; `$env:AGENT_EXECUTION_QUEUE_ENABLED='$queueEnabled'; .\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000"
        } else {
            Write-Host "AI Agent API is already listening on port 8000."
        }
        if ($redisReady) {
            $env:REDIS_URL = "redis://localhost:6379/0"
            $env:AGENT_EXECUTION_QUEUE_ENABLED = "true"
            $workerAlive = Test-AgentWorkerAlive $agentDir
            if (-not $workerAlive) {
                Write-Host "Starting Agent Worker..."
                Start-ServiceWindow "agent-worker" $agentDir "`$env:REDIS_URL='redis://localhost:6379/0'; `$env:AGENT_EXECUTION_QUEUE_ENABLED='true'; .\.venv\Scripts\python.exe -m rag.agent_execution_worker"
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
    Write-Host "Starting frontend-vue on port 5173..."
    Start-ServiceWindow "frontend-vue :5173" (Join-Path $Root "frontend-vue") "npm run dev"
} elseif (-not $SkipFrontend) {
    Write-Host "frontend-vue is already listening on port 5173."
}

Write-Host "Frontend: http://localhost:5173"
Write-Host "Agent:    http://localhost:8000/health"
Write-Host "Java:     http://localhost:8081/actuator/health"
Write-Host "Redis:    redis://localhost:6379/0"
