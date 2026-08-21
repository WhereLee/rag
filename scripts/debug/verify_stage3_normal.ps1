# 阶段 3 正常测试：分片上传（多片/断点续传/合并）/ 分片后秒传 / Range 下载 / 回归
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$stamp = Get-Date -Format "HHmmss"
$u = "s3_n_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xff = @{ "X-Forwarded-For" = "203.0.113.71" }
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

# 生成 25MB 测试文件（.txt 无魔数限制）
$bigPath = Join-Path $env:TEMP "s3_big_$stamp.txt"
$buf = [byte[]]::new($BIG_SIZE)
[System.IO.File]::WriteAllBytes($bigPath, $buf)
$hash = (Get-FileHash $bigPath -Algorithm SHA256).Hash.ToLower()
# 清理该 hash 的历史残留（测试内容固定 → hash 固定 → 重跑时保证环境干净）
Sql "DELETE FROM user_file WHERE blob_id IN (SELECT id FROM file_blob WHERE file_hash='$hash')" | Out-Null
Sql "DELETE FROM file_blob WHERE file_hash='$hash'" | Out-Null

# 注册 + 登录
PostJson "$base/api/auth/register" @{ username = $u; password = $passwd } $xff | Out-Null
$token = (PostJson "$base/api/auth/login" @{ username = $u; password = $passwd }).token
$headers = @{ Authorization = "Bearer $token" }
Check "注册+登录" ($token -ne "") "token empty"

# init 分片会话
$chunkCount = [int]($BIG_SIZE / $CHUNK)
$init = PostJson "$base/api/files/upload/init" @{ hash = $hash; size = $BIG_SIZE; filename = "big.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headers
Check "init 返回 upload_id" ($init.hit -eq $false -and $init.upload_id -gt 0) "resp=$($init | ConvertTo-Json)"
$uploadId = $init.upload_id

# 切片
$partDir = Join-Path $env:TEMP "s3_parts_$stamp"
New-Item -ItemType Directory -Path $partDir -Force | Out-Null
$fileBuf = [System.IO.File]::ReadAllBytes($bigPath)
for ($i = 0; $i -lt $chunkCount; $i++) {
    $start = $i * $CHUNK
    $len = [Math]::Min($CHUNK, $fileBuf.Length - $start)
    $part = [byte[]]::new($len)
    [Array]::Copy($fileBuf, $start, $part, 0, $len)
    [System.IO.File]::WriteAllBytes((Join-Path $partDir "$i.part"), $part)
}

# 传 2 片 → 查 status（断点续传）→ 传剩余 → complete
foreach ($i in 0, 1) {
    curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$(Join-Path $partDir "$i.part");filename=part" "$base/api/files/upload/$uploadId/chunk?index=$i" | Out-Null
}
$st = Invoke-RestMethod -Uri "$base/api/files/upload/$uploadId/status" -Headers $headers
$upList = @($st.uploaded)
Check "断点续传 status：已传 2 片" ($upList.Count -eq 2 -and $upList -contains 0 -and $upList -contains 1) "uploaded=$($st.uploaded -join ',')"
for ($i = 2; $i -lt $chunkCount; $i++) {
    curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$(Join-Path $partDir "$i.part");filename=part" "$base/api/files/upload/$uploadId/chunk?index=$i" | Out-Null
}
$done = PostJson "$base/api/files/upload/$uploadId/complete" @{} $headers
Check "complete 成功" ($done.id -gt 0) "resp=$($done | ConvertTo-Json)"
$fileId = $done.id

# 下载全量：字节一致
Invoke-WebRequest -Uri "$base/api/files/$fileId/download" -Headers $headers -OutFile "$env:TEMP\s3_dl.bin" | Out-Null
Check "分片文件下载字节一致" ((Get-Item "$env:TEMP\s3_dl.bin").Length -eq $BIG_SIZE) "len mismatch"

# 分片后秒传：同 hash init → hit=true
$init2 = PostJson "$base/api/files/upload/init" @{ hash = $hash; size = $BIG_SIZE; filename = "big2.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headers
Check "分片后秒传命中" ($init2.hit -eq $true) "resp=$($init2 | ConvertTo-Json)"

# Range 下载：0-99 → 206 + 100 字节 + Content-Range
$code = curl.exe -s -o "$env:TEMP\s3_range.bin" -w "%{http_code}" -H "Range: bytes=0-99" -H "Authorization: Bearer $token" "$base/api/files/$fileId/download"
Check "Range 返回 206" ($code -eq "206") "code=$code"
Check "Range 返回 100 字节" ((Get-Item "$env:TEMP\s3_range.bin").Length -eq 100) "len=$((Get-Item "$env:TEMP\s3_range.bin").Length)"
$respHeaders = curl.exe -s -D - -o NUL -H "Range: bytes=0-99" -H "Authorization: Bearer $token" "$base/api/files/$fileId/download"
$cr = $respHeaders | Select-String "Content-Range"
Check "Content-Range 头正确" ($cr -match "bytes 0-99/$BIG_SIZE") "header=$cr"

# 小文件整传回归
$smallPath = Join-Path $env:TEMP "s3_small_$stamp.txt"
Set-Content -Path $smallPath -Value "stage3 small file" -Encoding UTF8
$upSmall = curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$smallPath;filename=small.txt" "$base/api/files/upload" | ConvertFrom-Json
Check "小文件整传回归" ($upSmall.id -gt 0) "resp=$($upSmall | ConvertTo-Json)"

Remove-Item $bigPath, "$env:TEMP\s3_dl.bin", "$env:TEMP\s3_range.bin", $smallPath -ErrorAction SilentlyContinue
Remove-Item $partDir -Recurse -ErrorAction SilentlyContinue
Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段3 正常测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
