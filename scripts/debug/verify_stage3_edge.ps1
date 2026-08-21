# 阶段 3 边界测试：越权分片 / 分片缺失 / 超大小 / 非法序号 / hash 篡改 / Range 越界 / 超时会话清理
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$stamp = Get-Date -Format "HHmmss"
$ua = "s3_e_a_$stamp"; $ub = "s3_e_b_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xffA = @{ "X-Forwarded-For" = "203.0.113.81" }
$xffB = @{ "X-Forwarded-For" = "203.0.113.82" }
$CHUNK = 5MB
$BIG_SIZE = 25MB

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host "[OK] $name" } else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

function PostJson($url, $obj, $headers = @{}) {
    Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Headers $headers -Body ($obj | ConvertTo-Json)
}

function HttpStatus($scriptBlock) {
    try { & $scriptBlock | Out-Null; return 200 } catch { return $_.Exception.Response.StatusCode.value__ }
}

function Sql($sql) {
    $env:PGCLIENTENCODING = "UTF8"
    $dsn = (Get-Content (Join-Path $PSScriptRoot "..\..\.env") | Where-Object { $_ -match '^PG_DSN=' } | Select-Object -First 1) -replace '^PG_DSN=', ''
    & psql $dsn -t -c $sql 2>$null
}

function UploadChunk($token, $uploadId, $index, $path) {
    curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$path;filename=part" "$base/api/files/upload/$uploadId/chunk?index=$index"
}

# 期望 4xx 的 JSON 请求：curl 拿状态码 + body（Invoke-RestMethod 遇 4xx 会抛异常中止脚本）
function PostJsonCode($url, $obj, $headers = @{}) {
    $tmp = Join-Path $env:TEMP "s3_resp_$([guid]::NewGuid().ToString('N')).json"
    $auth = $headers['Authorization']
    $code = curl.exe -s -o $tmp -w "%{http_code}" -X POST -H "Content-Type: application/json; charset=utf-8" -H "Authorization: $auth" --data-binary ($obj | ConvertTo-Json -Compress) $url
    # PS 5.1 的 Get-Content 按 ANSI 解码会破坏 UTF-8 中文 → 用 .NET 显式 UTF-8 读
    $raw = $null
    if (Test-Path $tmp) { $raw = [System.IO.File]::ReadAllText($tmp, [System.Text.Encoding]::UTF8) }
    Remove-Item $tmp -ErrorAction SilentlyContinue
    $body = $null
    if ($raw) { try { $body = $raw | ConvertFrom-Json } catch { $body = $null } }
    return @{ code = [int]$code; body = $body }
}

# 期望 4xx 的分片请求：curl 拿状态码（curl 对 4xx 不抛异常）
function UploadChunkCode($token, $uploadId, $index, $path) {
    curl.exe -s -o NUL -w "%{http_code}" -X POST -H "Authorization: Bearer $token" -F "file=@$path;filename=part" "$base/api/files/upload/$uploadId/chunk?index=$index"
}

# 生成 25MB 文件 + 切片（全局复用）
$bigPath = Join-Path $env:TEMP "s3_big_$stamp.txt"
$buf = [byte[]]::new($BIG_SIZE)
[System.IO.File]::WriteAllBytes($bigPath, $buf)
$hash = (Get-FileHash $bigPath -Algorithm SHA256).Hash.ToLower()
# 清理该 hash 的历史残留（测试内容固定 → hash 固定 → 重跑时保证环境干净）
Sql "DELETE FROM user_file WHERE blob_id IN (SELECT id FROM file_blob WHERE file_hash='$hash')" | Out-Null
Sql "DELETE FROM file_blob WHERE file_hash='$hash'" | Out-Null
$chunkCount = [int]($BIG_SIZE / $CHUNK)
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

# B 专用文件：内容与全局不同（全局 hash 已被 A 入库 → B 用同内容 init 会秒传命中、
# 无 upload_id，非法序号/篡改测试就没有会话可测）
$bigPathB = Join-Path $env:TEMP "s3_bigB_$stamp.txt"
$bufB = [byte[]]::new($BIG_SIZE)
for ($i = 0; $i -lt $bufB.Length; $i += 4096) { $bufB[$i] = 0x42 }
[System.IO.File]::WriteAllBytes($bigPathB, $bufB)
$hashB = (Get-FileHash $bigPathB -Algorithm SHA256).Hash.ToLower()
$partDirB = Join-Path $env:TEMP "s3_partsB_$stamp"
New-Item -ItemType Directory -Path $partDirB -Force | Out-Null
$fileBufB = [System.IO.File]::ReadAllBytes($bigPathB)
for ($i = 0; $i -lt $chunkCount; $i++) {
    $start = $i * $CHUNK
    $len = [Math]::Min($CHUNK, $fileBufB.Length - $start)
    $part = [byte[]]::new($len)
    [Array]::Copy($fileBufB, $start, $part, 0, $len)
    [System.IO.File]::WriteAllBytes((Join-Path $partDirB "$i.part"), $part)
}

# 注册 A/B + 登录
PostJson "$base/api/auth/register" @{ username = $ua; password = $passwd } $xffA | Out-Null
PostJson "$base/api/auth/register" @{ username = $ub; password = $passwd } $xffB | Out-Null
$tokA = (PostJson "$base/api/auth/login" @{ username = $ua; password = $passwd }).token
$tokB = (PostJson "$base/api/auth/login" @{ username = $ub; password = $passwd }).token
$headersA = @{ Authorization = "Bearer $tokA" }
$headersB = @{ Authorization = "Bearer $tokB" }
Check "注册+登录 A/B" ($tokA -ne "" -and $tokB -ne "") "token empty"

# ---- 越权：A init，B 操作 A 的会话 ----
$init = PostJson "$base/api/files/upload/init" @{ hash = $hash; size = $BIG_SIZE; filename = "big.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headersA
$uploadId = $init.upload_id
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files/upload/$uploadId/status" -Headers $headersB }
Check "B 查 A 的会话 -> 404" ($st -eq 404) "status=$st"
$st = HttpStatus { UploadChunk $tokB $uploadId 0 (Join-Path $partDir "0.part") | Out-Null; Invoke-RestMethod -Uri "$base/api/files/upload/$uploadId/status" -Headers $headersB }
Check "B 传 A 的分片 -> 404" ($st -eq 404) "status=$st"
$st = HttpStatus { PostJson "$base/api/files/upload/$uploadId/complete" @{} $headersB }
Check "B complete A 的会话 -> 404" ($st -eq 404) "status=$st"

# ---- 分片缺失：A 传 3 片 → complete 拒绝 ----
foreach ($i in 0, 1, 2) { UploadChunk $tokA $uploadId $i (Join-Path $partDir "$i.part") | Out-Null }
$r = PostJsonCode "$base/api/files/upload/$uploadId/complete" @{} $headersA
Check "分片缺失 complete 拒绝" ($r.code -eq 400 -and $null -ne $r.body.error) "resp=$($r.body | ConvertTo-Json -Compress)"
# 补传剩余 2 片 → complete 成功（校验失败可重试的幂等设计）
foreach ($i in 3, 4) { UploadChunk $tokA $uploadId $i (Join-Path $partDir "$i.part") | Out-Null }
$r = PostJson "$base/api/files/upload/$uploadId/complete" @{} $headersA
Check "补齐分片后 complete 成功" ($r.id -gt 0) "resp=$($r | ConvertTo-Json)"

# ---- 非法参数 / 分片序号 / 超大小 ----
$st = HttpStatus { PostJson "$base/api/files/upload/init" @{ hash = $hash; size = 0; filename = "x.txt"; chunk_count = 1; chunk_size = $CHUNK } $headersB }
Check "size=0 -> 400" ($st -eq 400) "status=$st"
$st = HttpStatus { PostJson "$base/api/files/upload/init" @{ hash = $hash; size = $BIG_SIZE; filename = "x.txt"; chunk_count = 0; chunk_size = $CHUNK } $headersB }
Check "chunk_count=0 -> 400" ($st -eq 400) "status=$st"
$st = HttpStatus { PostJson "$base/api/files/upload/init" @{ hash = "zz"; size = $BIG_SIZE; filename = "x.txt"; chunk_count = 1; chunk_size = $CHUNK } $headersB }
Check "非法 hash -> 400" ($st -eq 400) "status=$st"
$initB = PostJson "$base/api/files/upload/init" @{ hash = $hashB; size = $BIG_SIZE; filename = "b.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headersB
$code = UploadChunkCode $tokB $initB.upload_id -1 (Join-Path $partDirB "0.part")
Check "index=-1 -> 400" ($code -eq "400") "code=$code"
$code = UploadChunkCode $tokB $initB.upload_id 5 (Join-Path $partDirB "0.part")
Check "index=chunk_count -> 400" ($code -eq "400") "code=$code"
# 超大小分片：6MB 片（chunk_size=5MB）
$oversize = Join-Path $env:TEMP "s3_over_$stamp.bin"
[System.IO.File]::WriteAllBytes($oversize, [byte[]]::new(6MB))
$code = UploadChunkCode $tokB $initB.upload_id 0 $oversize
Check "分片超大小 -> 400" ($code -eq "400") "code=$code"
Remove-Item $oversize -ErrorAction SilentlyContinue

# ---- hash 篡改：改 session.file_hash → complete 拒绝 ----
$initC = PostJson "$base/api/files/upload/init" @{ hash = $hashB; size = $BIG_SIZE; filename = "c.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headersB
Sql "UPDATE upload_session SET file_hash=repeat('0',64) WHERE id=$($initC.upload_id)" | Out-Null
foreach ($i in 0..($chunkCount - 1)) { UploadChunk $tokB $initC.upload_id $i (Join-Path $partDirB "$i.part") | Out-Null }
$r = PostJsonCode "$base/api/files/upload/$($initC.upload_id)/complete" @{} $headersB
Check "hash 篡改 complete 拒绝" ($r.code -eq 400 -and $null -ne $r.body.error) "resp=$($r.body | ConvertTo-Json -Compress)"

# ---- Range 越界 / 非法 ----
$fileId = (Invoke-RestMethod -Uri "$base/api/files" -Headers $headersA).items[0].id
$code = curl.exe -s -o NUL -w "%{http_code}" -H "Range: bytes=99999999999-" -H "Authorization: Bearer $tokA" "$base/api/files/$fileId/download"
Check "Range 越界 -> 416" ($code -eq "416") "code=$code"
$code = curl.exe -s -o NUL -w "%{http_code}" -H "Range: bytes=abc" -H "Authorization: Bearer $tokA" "$base/api/files/$fileId/download"
Check "Range 非法格式 -> 416" ($code -eq "416") "code=$code"

# ---- 超时会话清理：init 不传片 → 改旧 → cleanup ----
$initD = PostJson "$base/api/files/upload/init" @{ hash = $hashB; size = $BIG_SIZE; filename = "d.txt"; chunk_count = $chunkCount; chunk_size = $CHUNK } $headersB
Sql "UPDATE upload_session SET updated_at = now() - interval '2 hours' WHERE id=$($initD.upload_id)" | Out-Null
$cl = Invoke-RestMethod -Uri "$base/api/files/cleanup" -Method Post -Headers $headersA
Check "超时会话被清理" ($cl.session_cleaned -ge 1) "cleaned=$($cl.session_cleaned)"
$rows = Sql "SELECT count(*) FROM upload_session WHERE id=$($initD.upload_id)"
Check "会话记录已删" ($rows.Trim() -eq "0") "rows=$rows"

Remove-Item $bigPath, $bigPathB -ErrorAction SilentlyContinue
Remove-Item $partDir, $partDirB -Recurse -ErrorAction SilentlyContinue
Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段3 边界测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
