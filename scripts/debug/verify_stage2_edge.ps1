# 阶段 2 边界测试：秒传未命中 / hash 校验 / 并发上传去重 / 并发删除 / 恢复过期 / NFD 归一化 / 越权
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$projectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$stamp = Get-Date -Format "HHmmss"
$uc = "s2_e_c_$stamp"; $ud = "s2_e_d_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xffC = @{ "X-Forwarded-For" = "203.0.113.61" }
$xffD = @{ "X-Forwarded-For" = "203.0.113.62" }

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host "[OK] $name" } else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

function PostJson($url, $obj, $headers = @{}) {
    Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Headers $headers -Body ($obj | ConvertTo-Json)
}

function HttpStatus($scriptBlock) {
    try { & $scriptBlock | Out-Null; return 200 } catch { return $_.Exception.Response.StatusCode.value__ }
}

function Upload($token, $path, $filename) {
    $r = curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$path;filename=$filename" "$base/api/files/upload"
    $r | ConvertFrom-Json
}

function Sql($sql) {
    $env:PGCLIENTENCODING = "UTF8"
    $dsn = (Get-Content (Join-Path $projectRoot ".env") | Where-Object { $_ -match '^PG_DSN=' } | Select-Object -First 1) -replace '^PG_DSN=', ''
    & psql $dsn -t -c $sql 2>$null
}

# 注册 C/D + 登录
PostJson "$base/api/auth/register" @{ username = $uc; password = $passwd } $xffC | Out-Null
PostJson "$base/api/auth/register" @{ username = $ud; password = $passwd } $xffD | Out-Null
$loginC = PostJson "$base/api/auth/login" @{ username = $uc; password = $passwd }
$tokC = $loginC.token
$tokD = (PostJson "$base/api/auth/login" @{ username = $ud; password = $passwd }).token
$headersC = @{ Authorization = "Bearer $tokC" }
$headersD = @{ Authorization = "Bearer $tokD" }
Check "注册+登录 C/D" ($tokC -ne "" -and $tokD -ne "") "token empty"

# 准备测试文件：同内容两文件（hash 同 size 同）、size 不同版本
$tmpA = Join-Path $env:TEMP ("s2_same_" + $stamp + ".bin")
$tmpB = Join-Path $env:TEMP ("s2_diff_" + $stamp + ".bin")
$bytes = 1..1024 | ForEach-Object { 0x41 }  # 1KB 全 A
[System.IO.File]::WriteAllBytes($tmpA, [byte[]]$bytes)
$bytes2 = 1..512 | ForEach-Object { 0x41 }  # 512B 同内容开头（size 不同）
[System.IO.File]::WriteAllBytes($tmpB, [byte[]]$bytes2)
$hashA = (Get-FileHash $tmpA -Algorithm SHA256).Hash.ToLower()
$hashB = (Get-FileHash $tmpB -Algorithm SHA256).Hash.ToLower()
# 清理历史残留（测试内容固定 → hash 固定 → 重跑时保证环境干净）
Sql "DELETE FROM user_file WHERE blob_id IN (SELECT id FROM file_blob WHERE file_hash IN ('$hashA','$hashB'))" | Out-Null
Sql "DELETE FROM file_blob WHERE file_hash IN ('$hashA','$hashB')" | Out-Null

# ---- 秒传未命中 & hash 校验 ----
$chk = PostJson "$base/api/files/check-hash" @{ hash = $hashA; size = (Get-Item $tmpA).Length; filename = "x.bin" } $headersC
Check "未命中返回 hit=false" ($chk.hit -eq $false) "resp=$($chk | ConvertTo-Json)"
$st = HttpStatus { PostJson "$base/api/files/check-hash" @{ hash = "zzz"; size = 10; filename = "x.bin" } $headersC }
Check "非法 hash -> 400" ($st -eq 400) "status=$st"

# C 上传 tmpA → 再查 hashA 命中；hashB（同内容前缀）不命中
$upC = Upload $tokC $tmpA "same.bin"
$chk2 = PostJson "$base/api/files/check-hash" @{ hash = $hashA; size = (Get-Item $tmpA).Length; filename = "same2.bin" } $headersC
Check "上传后 check-hash 命中" ($chk2.hit -eq $true) "resp=$($chk2 | ConvertTo-Json)"
$chk3 = PostJson "$base/api/files/check-hash" @{ hash = $hashB; size = (Get-Item $tmpB).Length; filename = "short.bin" } $headersC
Check "hash 同前缀但 size 不同 -> 不命中" ($chk3.hit -eq $false) "resp=$($chk3 | ConvertTo-Json)"

# 并发测试前删掉 C 的前置引用（same.bin + 秒传的 same2.bin），保证引用计数从 0 开始
Invoke-RestMethod -Uri "$base/api/files/$($upC.id)" -Method Delete -Headers $headersC | Out-Null
Invoke-RestMethod -Uri "$base/api/files/$($chk2.id)" -Method Delete -Headers $headersC | Out-Null

# ---- 并发上传同内容：C/D 同时传 → 都成功且 blob 唯一 ----
$jobC = Start-Job -ScriptBlock {
    param($base, $token, $path)
    curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$path;filename=cc.bin" "$base/api/files/upload"
} -ArgumentList $base, $tokC, $tmpA
$jobD = Start-Job -ScriptBlock {
    param($base, $token, $path)
    curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$path;filename=dd.bin" "$base/api/files/upload"
} -ArgumentList $base, $tokD, $tmpA
$rC = Receive-Job $jobC -Wait | ConvertFrom-Json
$rD = Receive-Job $jobD -Wait | ConvertFrom-Json
Remove-Job $jobC, $jobD
Check "并发上传双方都成功" ($rC.id -ne $null -and $rD.id -ne $null) "C=$($rC.id) D=$($rD.id)"
$blobCount = Sql "SELECT count(*) FROM file_blob WHERE file_hash='$hashA'"
Check "并发同内容 blob 唯一" ($blobCount.Trim() -eq "1") "count=$blobCount"

# ---- 并发删除：C 删一次 → ref 减一；D 再删 → 归零删物理 ----
Invoke-RestMethod -Uri "$base/api/files/$($rC.id)" -Method Delete -Headers $headersC | Out-Null
$refCount = Sql "SELECT ref_count FROM file_blob WHERE file_hash='$hashA'"
Check "并发删除后 ref=1" ($refCount.Trim() -eq "1") "ref=$refCount"
$dlId = $rD.id
Invoke-RestMethod -Uri "$base/api/files/$dlId" -Method Delete -Headers $headersD | Out-Null
$refCount = Sql "SELECT ref_count FROM file_blob WHERE file_hash='$hashA'"
Check "最后引用删除后 blob 记录消失" ($refCount.Trim() -eq "") "ref=$refCount"

# ---- 恢复过期文件：D 传一个 → 删 → psql 删 blob → 恢复 -> 400 ----
$upD = Upload $tokD $tmpB "expire.bin"
Invoke-RestMethod -Uri "$base/api/files/$($upD.id)" -Method Delete -Headers $headersD | Out-Null
Sql "DELETE FROM file_blob WHERE file_hash='$hashB'" | Out-Null
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files/$($upD.id)/restore" -Method Post -Headers $headersD }
Check "恢复已清理文件 -> 400" ($st -eq 400) "status=$st"

# ---- NFD 文件名归一化：rename 传 NFD 名（PS 5.1 Invoke-RestMethod 发送 body 非 UTF-8，改用 Python requests 保证 UTF-8 JSON 传输）→ 列表显示 NFC ----
$tmpTxt = Join-Path $env:TEMP ("s2_nfd_" + $stamp + ".txt")
Set-Content -Path $tmpTxt -Value "nfd test" -Encoding UTF8
$upNfd = Upload $tokC $tmpTxt "ascii.txt"
Check "NFD 测试文件上传成功" ($upNfd.id -ne $null) "resp=$($upNfd | ConvertTo-Json)"
$pyScript = @"
import requests
r = requests.put('$base/api/files/$($upNfd.id)/rename',
                 headers={'Authorization': 'Bearer $tokC'},
                 json={'filename': 'cafe\u0301.txt'})
print(r.status_code)
"@
$renameStatus = ($pyScript | python -).Trim()
Check "NFD 名 rename 成功" ($renameStatus -eq "200") "status=$renameStatus"
# 列表验证用 Python：PS 5.1 Invoke-RestMethod 按 ANSI 解码 UTF-8 JSON 响应，非 ASCII 断言必须避开
$pyVerify = @"
import requests
r = requests.get('$base/api/files?pageSize=100', headers={'Authorization': 'Bearer $tokC'})
items = r.json()['items']
f = next((x for x in items if x['id'] == $($upNfd.id)), None)
print('OK' if f and f['filename'] == 'caf\u00e9.txt' else repr(f['filename'] if f else None))
"@
$verifyResult = ($pyVerify | python -).Trim()
Check "列表显示 NFC 归一化文件名" ($verifyResult -eq "OK") "result=$verifyResult"

# ---- 越权恢复：D 恢复 C 回收站的文件 -> 404 ----
Invoke-RestMethod -Uri "$base/api/files/$($upNfd.id)" -Method Delete -Headers $headersC | Out-Null
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files/$($upNfd.id)/restore" -Method Post -Headers $headersD }
Check "D 恢复 C 的文件 -> 404" ($st -eq 404) "status=$st"

Remove-Item $tmpA, $tmpB, $tmpTxt -ErrorAction SilentlyContinue
Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段2 边界测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
