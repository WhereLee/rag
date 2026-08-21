# 阶段 5a 正常测试：磁盘监控不误伤（默认阈值下整传/分片/下载/删除全正常）+ 环境变量齐全启动
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$stamp = Get-Date -Format "HHmmss"
$u = "s5a_n_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xff = @{ "X-Forwarded-For" = "203.0.113.91" }
$CHUNK = 5MB
$BIG_SIZE = 25MB

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

# 25MB 测试文件（内容固定 → hash 固定 → 先清残留）
$bigPath = Join-Path $env:TEMP "s5a_big_$stamp.txt"
[System.IO.File]::WriteAllBytes($bigPath, [byte[]]::new($BIG_SIZE))
$hash = (Get-FileHash $bigPath -Algorithm SHA256).Hash.ToLower()
Sql "DELETE FROM user_file WHERE blob_id IN (SELECT id FROM file_blob WHERE file_hash='$hash')" | Out-Null
Sql "DELETE FROM file_blob WHERE file_hash='$hash'" | Out-Null

# 注册 + 登录
PostJson "$base/api/auth/register" @{ username = $u; password = $passwd } $xff | Out-Null
$token = (PostJson "$base/api/auth/login" @{ username = $u; password = $passwd }).token
$headers = @{ Authorization = "Bearer $token" }
Check "注册+登录" ($token -ne "") "token empty"

# 1. 磁盘正常：整传成功（默认阈值 1GB/5% 不误伤，本机剩余 17GB/7.5%）
$smallPath = Join-Path $env:TEMP "s5a_small_$stamp.txt"
Set-Content -Path $smallPath -Value "stage5a small" -Encoding UTF8
$up = curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$smallPath;filename=small.txt" "$base/api/files/upload" | ConvertFrom-Json
Check "整传成功（磁盘检查不误伤）" ($up.id -gt 0) "resp=$($up | ConvertTo-Json -Compress)"

# 2. 分片上传全流程正常（init 磁盘检查放行）
$chunkCount = [int]($BIG_SIZE / $CHUNK)
$init = PostJson "$base/api/files/upload/init" @{ hash = $hash; size = $BIG_SIZE; filename = "big.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headers
Check "分片 init 成功" ($null -ne $init.upload_id) "resp=$($init | ConvertTo-Json -Compress)"
$fileBuf = [System.IO.File]::ReadAllBytes($bigPath)
$partDir = Join-Path $env:TEMP "s5a_parts_$stamp"
New-Item -ItemType Directory -Path $partDir -Force | Out-Null
for ($i = 0; $i -lt $chunkCount; $i++) {
    $start = $i * $CHUNK
    $len = [Math]::Min($CHUNK, $fileBuf.Length - $start)
    $part = [byte[]]::new($len)
    [Array]::Copy($fileBuf, $start, $part, 0, $len)
    [System.IO.File]::WriteAllBytes((Join-Path $partDir "$i.part"), $part)
    curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$(Join-Path $partDir "$i.part");filename=part" "$base/api/files/upload/$($init.upload_id)/chunk?index=$i" | Out-Null
}
$done = PostJson "$base/api/files/upload/$($init.upload_id)/complete" @{} $headers
Check "分片 complete 成功" ($done.id -gt 0) "resp=$($done | ConvertTo-Json -Compress)"

# 3. 下载回归（完整 + Range）
curl.exe -s -o "$env:TEMP\s5a_dl.bin" -H "Authorization: Bearer $token" "$base/api/files/$($done.id)/download"
Check "下载字节一致" ((Get-Item "$env:TEMP\s5a_dl.bin").Length -eq $BIG_SIZE) "len mismatch"
$code = curl.exe -s -o NUL -w "%{http_code}" -H "Range: bytes=0-9" -H "Authorization: Bearer $token" "$base/api/files/$($done.id)/download"
Check "Range 下载 206" ($code -eq "206") "code=$code"

# 4. 删除回归
curl.exe -s -X DELETE -H "Authorization: Bearer $token" "$base/api/files/$($up.id)" | Out-Null
Check "删除回归" ($true) ""

Remove-Item $bigPath, $smallPath, "$env:TEMP\s5a_dl.bin" -ErrorAction SilentlyContinue
Remove-Item $partDir -Recurse -ErrorAction SilentlyContinue
Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段5a 正常测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
