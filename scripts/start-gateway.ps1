# 启动 Java 网关：从根 .env 读取变量注入环境后启动。
# 用途：Spring Boot 不读 .env，安全配置（GATEWAY_JWT_SECRET / SPRING_DATASOURCE_PASSWORD /
# GATEWAY_INTERNAL_API_KEY）缺失时网关会 fail-fast，必须先注入。
# 用法：powershell -File scripts\start-gateway.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) { Write-Error "未找到 $envFile" }

# 从 .env 注入：已显式设置的进程环境变量优先（测试/部署时可覆盖 .env 值）
foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
    $line = $line.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { continue }
    $idx = $line.IndexOf("=")
    if ($idx -le 0) { continue }
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()
    if (-not [System.Environment]::GetEnvironmentVariable($key, "Process")) {
        [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
    }
}

# 缺失校验：缺了网关会 fail-fast，这里提前给清晰提示（INTERNAL_API_KEY 与 GATEWAY_INTERNAL_API_KEY 二选一）
$missing = @()
if (-not [System.Environment]::GetEnvironmentVariable("GATEWAY_JWT_SECRET", "Process")) { $missing += "GATEWAY_JWT_SECRET" }
if (-not [System.Environment]::GetEnvironmentVariable("SPRING_DATASOURCE_PASSWORD", "Process")) { $missing += "SPRING_DATASOURCE_PASSWORD" }
if (-not [System.Environment]::GetEnvironmentVariable("GATEWAY_INTERNAL_API_KEY", "Process") -and -not [System.Environment]::GetEnvironmentVariable("INTERNAL_API_KEY", "Process")) { $missing += "GATEWAY_INTERNAL_API_KEY（或 INTERNAL_API_KEY）" }
if ($missing.Count -gt 0) {
    Write-Error "缺失安全配置: $($missing -join ', ')（请在 .env 中补充）"
}

Write-Host "gateway starting with env from .env ..."
Set-Location $root
mvn -q -f rag-java/pom.xml spring-boot:run
