# 阶段 5a 边界测试：磁盘阈值拒绝（整传/init/complete）/ 恢复 / complete 幂等重试 / 缺配置 fail-fast
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$stamp = Get-Date -Format "HHmmss"
$u = "s5a_e_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xff = @{ "X-Forwarded-For" = "203.0.113.92" }
$CHUNK = 5MB
$BIG_SIZE = 25MB
$HUGE = "1099511627776"   # 1TB 最小剩余空间 → 必触发磁盘拒绝
$gwLog = Join-Path (Join-Path $PSScriptRoot "..\..\logs") "gateway-test.log"
$root = Join-Path $PSScriptRoot "..\.."

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host "[OK] $name" } else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

function PostJson($url, $obj, $headers = @{}) {
    Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Headers $headers -Body ($obj | ConvertTo-Json)
}

function Sql($sql) {
    $env:PGCLIENTENCODING = "UTF8"
    $dsn = (Get-Content (Join-Path $PSScriptRoot "..\..\.env") | Where-Object { $_ -match '^PG_DSN=' } | Select-Object -First 1) -replace '^PG_DSN=', ''
    & psql $dsn -t -c $sql 2>$null
}

function StopGateway {
    $conn = Get-NetTCPConnection -LocalPort 8082 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 3 }
    # 启动脚本是父 powershell，mvn 是子进程；停掉监听进程后父进程会退出，等 2 秒兜底
    Start-Sleep -Seconds 2
}

function StartGateway {
    Remove-Item $gwLog -ErrorAction SilentlyContinue
    $proc = Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","$root\scripts\start-gateway.ps1" -RedirectStandardOutput $gwLog -RedirectStandardError "$gwLog.err" -PassThru -WindowStyle Hidden
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        $conn = Get-NetTCPConnection -LocalPort 8082 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) { $ready = $true; break }
        if ($proc.HasExited) { break }
    }
    return $ready
}

# ---- 前置：生成 25MB 文件 + 切片 ----
$bigPath = Join-Path $env:TEMP "s5a_big_$stamp.txt"
[System.IO.File]::WriteAllBytes($bigPath, [byte[]]::new($BIG_SIZE))
$hash = (Get-FileHash $bigPath -Algorithm SHA256).Hash.ToLower()
Sql "DELETE FROM user_file WHERE blob_id IN (SELECT id FROM file_blob WHERE file_hash='$hash')" | Out-Null
Sql "DELETE FROM file_blob WHERE file_hash='$hash'" | Out-Null
$chunkCount = [int]($BIG_SIZE / $CHUNK)
$partDir = Join-Path $env:TEMP "s5a_parts_$stamp"
New-Item -ItemType Directory -Path $partDir -Force | Out-Null
$fileBuf = [System.IO.File]::ReadAllBytes($bigPath)
for ($i = 0; $i -lt $chunkCount; $i++) {
    $start = $i * $CHUNK
    $len = [Math]::Min($CHUNK, $fileBuf.Length - $start)
    $part = [byte[]]::new($len)
    [Array]::Copy($fileBuf, $start, $part, 0, $len)
    [System.IO.File]::WriteAllBytes((Join-Path $partDir "$i.part"), $part)
}

# 注册 + 登录
PostJson "$base/api/auth/register" @{ username = $u; password = $passwd } $xff | Out-Null
$token = (PostJson "$base/api/auth/login" @{ username = $u; password = $passwd }).token
$headers = @{ Authorization = "Bearer $token" }
Check "注册+登录" ($token -ne "") "token empty"

$smallPath = Join-Path $env:TEMP "s5a_small_$stamp.txt"
Set-Content -Path $smallPath -Value "stage5a edge" -Encoding UTF8

# ---- 1. 大阈值重启 → 整传/init 被拒 ----
StopGateway
$env:GATEWAY_UPLOAD_MIN_FREE_BYTES = $HUGE
$ok = StartGateway
Check "大阈值启动成功" $ok "gateway not ready"
$code = curl.exe -s -o NUL -w "%{http_code}" -X POST -H "Authorization: Bearer $token" -F "file=@$smallPath;filename=small.txt" "$base/api/files/upload"
Check "磁盘不足整传 400" ($code -eq "400") "code=$code"
$st = 0
try { PostJson "$base/api/files/upload/init" @{ hash = $hash; size = $BIG_SIZE; filename = "big.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headers | Out-Null; $st = 200 } catch { $st = $_.Exception.Response.StatusCode.value__ }
Check "磁盘不足 init 400" ($st -eq 400) "status=$st"

# ---- 2. 恢复阈值 → 上传成功（回归）----
StopGateway
Remove-Item Env:GATEWAY_UPLOAD_MIN_FREE_BYTES -ErrorAction SilentlyContinue
$ok = StartGateway
Check "恢复阈值启动成功" $ok "gateway not ready"
$up = curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$smallPath;filename=small.txt" "$base/api/files/upload" | ConvertFrom-Json
Check "恢复后整传成功" ($up.id -gt 0) "resp=$($up | ConvertTo-Json -Compress)"

# ---- 3. 分片 complete 磁盘检查 + 幂等重试：正常传完片 → 大阈值下 complete 400 → 恢复后 complete 200 ----
$init = PostJson "$base/api/files/upload/init" @{ hash = $hash; size = $BIG_SIZE; filename = "big.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headers
Check "分片 init 成功" ($null -ne $init.upload_id) "resp=$($init | ConvertTo-Json -Compress)"
for ($i = 0; $i -lt $chunkCount; $i++) {
    curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$(Join-Path $partDir "$i.part");filename=part" "$base/api/files/upload/$($init.upload_id)/chunk?index=$i" | Out-Null
}
StopGateway
$env:GATEWAY_UPLOAD_MIN_FREE_BYTES = $HUGE
$ok = StartGateway
Check "大阈值二次启动成功" $ok "gateway not ready"
$st = 0
try { PostJson "$base/api/files/upload/$($init.upload_id)/complete" @{} $headers | Out-Null; $st = 200 } catch { $st = $_.Exception.Response.StatusCode.value__ }
Check "磁盘不足 complete 400" ($st -eq 400) "status=$st"
StopGateway
Remove-Item Env:GATEWAY_UPLOAD_MIN_FREE_BYTES -ErrorAction SilentlyContinue
$ok = StartGateway
Check "恢复阈值二次启动成功" $ok "gateway not ready"
$done = PostJson "$base/api/files/upload/$($init.upload_id)/complete" @{} $headers
Check "恢复后 complete 幂等重试成功" ($done.id -gt 0) "resp=$($done | ConvertTo-Json -Compress)"
curl.exe -s -o "$env:TEMP\s5a_dl.bin" -H "Authorization: Bearer $token" "$base/api/files/$($done.id)/download"
Check "重试后文件下载一致" ((Get-Item "$env:TEMP\s5a_dl.bin").Length -eq $BIG_SIZE) "len mismatch"

# ---- 4. fail-fast：JWT secret 置为空白 → 启动失败且日志提示缺失 ----
StopGateway
$env:GATEWAY_JWT_SECRET = " "   # 空格：启动脚本校验通过（非空），Spring 注入后 isBlank → fail-fast
Remove-Item $gwLog -ErrorAction SilentlyContinue
$proc = Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","$root\scripts\start-gateway.ps1" -RedirectStandardOutput $gwLog -RedirectStandardError "$gwLog.err" -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 25
$conn = Get-NetTCPConnection -LocalPort 8082 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
Check "缺配置启动失败（8082 未监听）" ($null -eq $conn) "port still listening"
$logText = ""
if (Test-Path $gwLog) { $logText = [System.IO.File]::ReadAllText($gwLog, [System.Text.Encoding]::UTF8) }
if (Test-Path "$gwLog.err") { $logText += [System.IO.File]::ReadAllText("$gwLog.err", [System.Text.Encoding]::UTF8) }
Check "日志包含缺失配置提示" ($logText -match "GATEWAY_JWT_SECRET") "log=$($logText.Substring(0, [Math]::Min(300, $logText.Length)))"

# ---- 5. 恢复完整配置启动（保证环境干净）----
StopGateway
Remove-Item Env:GATEWAY_JWT_SECRET -ErrorAction SilentlyContinue
$ok = StartGateway
Check "恢复配置启动成功" $ok "gateway not ready"

Remove-Item $bigPath, $smallPath, "$env:TEMP\s5a_dl.bin" -ErrorAction SilentlyContinue
Remove-Item $partDir -Recurse -ErrorAction SilentlyContinue
Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段5a 边界测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
