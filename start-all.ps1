param(
    [switch]$SkipDocker,
    [switch]$SkipJava,
    [switch]$SkipAgent,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 本机已安装 Maven 但未必写入系统 PATH；启动脚本先补入常见安装目录，保证 business-service 可启动。
$BundledMavenBin = "D:\ShiXun\apache-maven-3.9.8\bin"
if ((Test-Path "D:\ShiXun\apache-maven-3.9.8\bin\mvn.cmd") -and ($env:Path -notlike "*D:\ShiXun\apache-maven-3.9.8\bin*")) {
    $env:Path = "$BundledMavenBin;$env:Path"
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-PortOpen {
    param([int]$Port)
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$connection
}

function Start-ServiceWindow {
    param(
        [string]$Title,
        [string]$WorkingDirectory,
        [string]$Command
    )

    $escapedTitle = $Title.Replace("'", "''")
    $escapedWorkdir = $WorkingDirectory.Replace("'", "''")
    $escapedCommand = $Command.Replace("'", "''")
    $script = @"
`$Host.UI.RawUI.WindowTitle = '$escapedTitle'
Set-Location '$escapedWorkdir'
$escapedCommand
"@

    Start-Process powershell.exe `
        -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $script `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Normal
}

function Test-DockerReady {
    try {
        $oldPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & docker info *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $oldPreference
    }
}

function Start-DockerDesktopIfPossible {
    $candidates = @(
        "C:\Program Files\Docker\Docker\Docker Desktop.exe",
        "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            Write-Host "Starting Docker Desktop..."
            Start-Process $candidate | Out-Null
            return $true
        }
    }

    return $false
}

function Wait-DockerReady {
    param([int]$TimeoutSeconds = 90)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-DockerReady) {
            return $true
        }
        Start-Sleep -Seconds 3
    }

    return $false
}

Write-Host "== Enterprise Customer Agent startup =="
Write-Host "Root: $Root"

if (-not $SkipDocker) {
    if (-not (Test-CommandExists "docker")) {
        Write-Warning "docker command not found. Skip pgvector container."
    } else {
        if (-not (Test-DockerReady)) {
            Write-Warning "Docker daemon is not running."
            if (Start-DockerDesktopIfPossible) {
                if (-not (Wait-DockerReady -TimeoutSeconds 90)) {
                    Write-Warning "Docker Desktop did not become ready in time. Skip pgvector container."
                    $SkipDocker = $true
                }
            } else {
                Write-Warning "Docker Desktop executable not found. Skip pgvector container."
                $SkipDocker = $true
            }
        }
    }
}

if (-not $SkipDocker) {
    if (-not (Test-CommandExists "docker")) {
        Write-Warning "docker command not found. Skip pgvector container."
    } else {
        Write-Host "Checking pgvector container..."
        $containerName = "customer-agent-pgvector"
        $existing = docker ps -a --filter "name=$containerName" --format "{{.Names}}" 2>$null

        if ($existing -contains $containerName) {
            $running = docker ps --filter "name=$containerName" --filter "status=running" --format "{{.Names}}" 2>$null
            if ($running -contains $containerName) {
                Write-Host "pgvector container is already running."
            } else {
                Write-Host "Starting existing pgvector container..."
                docker start $containerName | Out-Null
            }
        } else {
            Write-Host "Creating pgvector container..."
            docker run --name $containerName `
                -e POSTGRES_USER=postgres `
                -e POSTGRES_PASSWORD=postgres `
                -e POSTGRES_DB=customer_agent `
                -p 5432:5432 `
                -d pgvector/pgvector:pg16 | Out-Null
        }
    }
}

if (-not $SkipJava) {
    if (Test-PortOpen 8081) {
        Write-Host "business-service is already listening on 8081. Skip."
    } else {
        if (-not (Test-CommandExists "mvn")) {
            Write-Warning "mvn command not found. Cannot start business-service."
        } else {
            Start-ServiceWindow `
                -Title "business-service :8081" `
                -WorkingDirectory (Join-Path $Root "business-service") `
                -Command "mvn spring-boot:run"
            Write-Host "business-service window started."
        }
    }
}

if (-not $SkipAgent) {
    if (Test-PortOpen 8000) {
        Write-Host "ai-agent-service is already listening on 8000. Skip."
    } else {
        $agentDir = Join-Path $Root "ai-agent-service"
        $python = Join-Path $agentDir ".venv\Scripts\python.exe"
        if (-not (Test-Path $python)) {
            Write-Warning "ai-agent-service .venv not found. Create venv and install requirements first."
        } else {
            Start-ServiceWindow `
                -Title "ai-agent-service :8000" `
                -WorkingDirectory $agentDir `
                -Command ".\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000"
            Write-Host "ai-agent-service window started."
        }
    }
}

if (-not $SkipFrontend) {
    if (Test-PortOpen 5173) {
        Write-Host "frontend-vue is already listening on 5173. Skip."
    } else {
        if (-not (Test-CommandExists "npm")) {
            Write-Warning "npm command not found. Cannot start frontend-vue."
        } else {
            Start-ServiceWindow `
                -Title "frontend-vue :5173" `
                -WorkingDirectory (Join-Path $Root "frontend-vue") `
                -Command "npm run dev"
            Write-Host "frontend-vue window started."
        }
    }
}

Write-Host ""
Write-Host "URLs:"
Write-Host "  Vue frontend:  http://localhost:5173/"
Write-Host "  Agent API:     http://localhost:8000/health"
Write-Host "  Java service:  http://localhost:8081"
Write-Host ""
Write-Host "Options:"
Write-Host "  .\start-all.ps1 -SkipDocker"
Write-Host "  .\start-all.ps1 -SkipJava"
Write-Host "  .\start-all.ps1 -SkipAgent"
Write-Host "  .\start-all.ps1 -SkipFrontend"
