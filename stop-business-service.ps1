param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Join-Path $Root ".runtime"
$javaPidFile = Join-Path $runtimeDir "business-service.json"

function Test-JavaHealthy {
    # 仅用于停止后的结果确认，不以端口是否被其他程序占用作为停止依据。
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8081/actuator/health" -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (-not (Test-Path $javaPidFile)) {
    Write-Warning "未找到受管 Java 服务的 PID 文件：$javaPidFile"
    Write-Host "不会停止未知的 8081 占用进程。若服务是手动启动的，请在对应终端停止。"
    exit 0
}

try {
    $metadata = Get-Content -Raw -Path $javaPidFile | ConvertFrom-Json
    $process = Get-Process -Id ([int]$metadata.pid) -ErrorAction Stop
    # PID 可能被系统复用；只有启动时间一致时才允许结束进程树。
    $recordedStartedAt = ([datetime]$metadata.started_at).ToUniversalTime()
    $actualStartedAt = $process.StartTime.ToUniversalTime()
    if ([math]::Abs(($actualStartedAt - $recordedStartedAt).TotalSeconds) -gt 1) {
        throw "PID $($metadata.pid) 的启动时间与登记记录不一致，已拒绝停止。"
    }

    Write-Host "Stopping Java business-service (PID $($process.Id))..."
    # Maven 会派生 Java 子进程；/T 保证完整结束由脚本启动的进程树。
    & taskkill.exe /PID $process.Id /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "taskkill 执行失败，退出码：$LASTEXITCODE"
    }
    Remove-Item -Path $javaPidFile -Force -ErrorAction SilentlyContinue

    if (Test-JavaHealthy) {
        Write-Warning "8081 仍返回 Java 健康状态；可能存在另一个手动启动的业务服务。"
    } else {
        Write-Host "Java business-service has stopped."
    }
} catch {
    Write-Error "停止 Java business-service 失败：$($_.Exception.Message)"
    exit 1
}

