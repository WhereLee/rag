# R6 边界测试：解析失败/重试/越权/未解析 409/产物缺失/并发消费/崩溃恢复
# 前置：网关(8082) + parse worker 已启动
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$fail = 0
$dsn = (Get-Content "$PSScriptRoot\..\..\.env" | Where-Object { $_ -match '^PG_DSN=' } | Select-Object -First 1) -replace '^PG_DSN=',''

function Check($name, $cond, $detail = "") {
    if ($cond) { Write-Host "[OK] $name" }
    else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

function NewUser([string]$prefix) {
    $stamp = Get-Date -Format "HHmmssfff"
    $u = "${prefix}_$stamp"
    $xff = @{ "X-Forwarded-For" = "203.0.113.$((Get-Random -Minimum 10 -Maximum 90))" }
    Invoke-RestMethod -Uri "$base/api/auth/register" -Method Post -ContentType "application/json; charset=utf-8" `
        -Headers $xff -Body (@{ username = $u; password = "Passw0rd1" } | ConvertTo-Json) | Out-Null
    $tok = (Invoke-RestMethod -Uri "$base/api/auth/login" -Method Post -ContentType "application/json" `
        -Headers $xff -Body (@{ username = $u; password = "Passw0rd1" } | ConvertTo-Json)).token
    return @{ user = $u; token = $tok }
}

function UploadFile($u, [string]$path, [string]$filename) {
    $code = curl.exe -s -o $env:TEMP\r6e_out.json -w "%{http_code}" -X POST `
        -H "Authorization: Bearer $($u.token)" -F "file=@$path;filename=$filename" "$base/api/files/upload"
    $body = [System.IO.File]::ReadAllText("$env:TEMP\r6e_out.json", [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($code -ne "200") { throw "upload failed: $code $body" }
    return $body
}

function GetParseStatus($u, [int64]$fileId) {
    $list = Invoke-RestMethod -Uri "$base/api/files?page=1&pageSize=50" -Headers @{ "Authorization" = "Bearer $($u.token)" }
    $item = $list.items | Where-Object { $_.id -eq $fileId }
    return $item
}

function WaitParseNot([string]$st, [int]$timeoutSec = 20) {
    # 等待状态离开 $st（返回最新行）
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $row = psql -P pager=off $dsn -t -A -c "SELECT status FROM parse_tasks WHERE file_id=$st" 2>$null
        $cur = ($row | Select-Object -Last 1).Trim()
        if ($cur -and $cur -ne "parsing" -and $cur -ne $null) { return $cur }
        Start-Sleep -Seconds 2
    }
    return $null
}

$u = NewUser "r6e"
$tmp = Join-Path $env:TEMP "r6e_$((Get-Date -Format 'HHmmss'))"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

# 1. 空 txt：上传成功（无魔数限制）但解析空产物 → failed
$empty = Join-Path $tmp "empty.txt"
Set-Content -Path $empty -Value "" -Encoding UTF8
$f = UploadFile $u $empty "empty.txt"
$deadline = (Get-Date).AddSeconds(20)
$st = ""
while ((Get-Date) -lt $deadline) {
    $item = GetParseStatus $u ([int64]$f.id)
    if ($item -and $item.parse_status -eq "failed") { $st = $item; break }
    Start-Sleep -Seconds 2
}
Check "空文件解析 failed" ($null -ne $st) "timeout"
Check "failed 带错误原因" ($null -ne $st.parse_error -and $st.parse_error.Length -gt 0) $st.parse_error

# 2. 手动重试：failed → reparse（重置 attempt=0）→ pending → 再次消费（重新计数）
$code = curl.exe -s -o NUL -w "%{http_code}" -X POST -H "Authorization: Bearer $($u.token)" "$base/api/files/$($f.id)/reparse"
Check "reparse 成功(200)" ($code -eq "200") $code
Start-Sleep -Seconds 1
$row2 = psql -P pager=off $dsn -t -A -c "SELECT status||'/'||attempt FROM parse_tasks WHERE file_id=$($f.id)" 2>$null
$cur2 = ($row2 | Select-Object -Last 1).Trim()
Check "重试后 pending 且 attempt 重置" ($cur2 -eq "pending/0") $cur2
$deadline = (Get-Date).AddSeconds(20)
$st2 = ""
while ((Get-Date) -lt $deadline) {
    $row3 = psql -P pager=off $dsn -t -A -c "SELECT status||'/'||attempt FROM parse_tasks WHERE file_id=$($f.id)" 2>$null
    $cur3 = ($row3 | Select-Object -Last 1).Trim()
    if ($cur3 -like "failed/*") { $st2 = $cur3; break }
    Start-Sleep -Seconds 2
}
Check "重试后再次失败且重新计数" ($null -ne $st2 -and $st2 -eq "failed/1") $st2

# 3. 越权预览：B 用户预览 A 的文件 → 404
$uB = NewUser "r6eb"
$code = curl.exe -s -o NUL -w "%{http_code}" -X GET -H "Authorization: Bearer $($uB.token)" "$base/api/files/$($f.id)/preview"
Check "越权预览 404" ($code -eq "404") $code

# 4. 不存在文件预览 → 404
$code = curl.exe -s -o NUL -w "%{http_code}" -X GET -H "Authorization: Bearer $($u.token)" "$base/api/files/99999999/preview"
Check "不存在文件预览 404" ($code -eq "404") $code

# 5. 未解析完成预览 → 409：DB 直接构造 pending 且无产物
$okTxt = Join-Path $tmp "ok.txt"
Set-Content -Path $okTxt -Value "正常内容文件。" -Encoding UTF8
$fok = UploadFile $u $okTxt "ok.txt"
psql -P pager=off $dsn -c "UPDATE parse_tasks SET status='pending', updated_at=now() WHERE file_id=$($fok.id)" | Out-Null
Remove-Item -Path "$PSScriptRoot\..\..\data\parsed\$($fok.id).json" -ErrorAction SilentlyContinue
$code = curl.exe -s -o NUL -w "%{http_code}" -X GET -H "Authorization: Bearer $($u.token)" "$base/api/files/$($fok.id)/preview"
Check "未解析预览 409" ($code -eq "409") $code

# 6. 产物缺失 → 404：DB 置 success 但删产物文件
psql -P pager=off $dsn -c "UPDATE parse_tasks SET status='success', updated_at=now() WHERE file_id=$($fok.id)" | Out-Null
Remove-Item -Path "$PSScriptRoot\..\..\data\parsed\$($fok.id).json" -ErrorAction SilentlyContinue
$code = curl.exe -s -o NUL -w "%{http_code}" -X GET -H "Authorization: Bearer $($u.token)" "$base/api/files/$($fok.id)/preview"
Check "产物缺失预览 404" ($code -eq "404") $code

# 7. 并发/重复消费：两个 pending 任务都被消费（不重复）
$fA = UploadFile $u (Join-Path $tmp "ok.txt") "okA.txt"
$fB = UploadFile $u (Join-Path $tmp "ok.txt") "okB.txt"
$deadline = (Get-Date).AddSeconds(25)
$done = $false
while ((Get-Date) -lt $deadline) {
    $rows = psql -P pager=off $dsn -t -A -c "SELECT file_id, status FROM parse_tasks WHERE file_id IN ($($fA.id), $($fB.id)) ORDER BY file_id" 2>$null
    $allDone = ($rows | Where-Object { $_ -notmatch "parsing" -and $_ -notmatch "pending" }).Count -eq 2
    if ($allDone) { $done = $true; break }
    Start-Sleep -Seconds 2
}
Check "双任务均被消费" $done $rows

# 8. 崩溃恢复：parsing + 旧时间戳 → 回收为 pending → 被消费
$fC = UploadFile $u (Join-Path $tmp "ok.txt") "okC.txt"
psql -P pager=off $dsn -c "UPDATE parse_tasks SET status='parsing', updated_at=now() - interval '10 minutes' WHERE file_id=$($fC.id)" | Out-Null
$deadline = (Get-Date).AddSeconds(20)
$recovered = $false
while ((Get-Date) -lt $deadline) {
    $row = psql -P pager=off $dsn -t -A -c "SELECT status FROM parse_tasks WHERE file_id=$($fC.id)" 2>$null
    $cur = ($row | Select-Object -Last 1).Trim()
    if ($cur -eq "success" -or $cur -eq "failed" -or $cur -eq "partial") { $recovered = $true; break }
    Start-Sleep -Seconds 2
}
Check "崩溃任务回收并消费" $recovered $cur

Write-Host ""
if ($fail -eq 0) { Write-Host "R6 边界测试: 全部通过" } else { Write-Host "R6 边界测试: $fail 项失败" }
exit $fail
