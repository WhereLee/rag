# R6 正常测试：上传自动入队 → worker 解析 → 状态流转 → 预览闭环
# 前置：网关(8082) + parse worker 已启动
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$fail = 0

function Check($name, $cond, $detail = "") {
    if ($cond) { Write-Host "[OK] $name" }
    else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

function NewUser([string]$prefix) {
    $stamp = Get-Date -Format "HHmmssfff"
    $u = "${prefix}_$stamp"
    $xff = @{ "X-Forwarded-For" = "198.51.100.$((Get-Random -Minimum 10 -Maximum 90))" }
    Invoke-RestMethod -Uri "$base/api/auth/register" -Method Post -ContentType "application/json; charset=utf-8" `
        -Headers $xff -Body (@{ username = $u; password = "Passw0rd1" } | ConvertTo-Json) | Out-Null
    $tok = (Invoke-RestMethod -Uri "$base/api/auth/login" -Method Post -ContentType "application/json" `
        -Headers $xff -Body (@{ username = $u; password = "Passw0rd1" } | ConvertTo-Json)).token
    return @{ user = $u; token = $tok; xff = $xff }
}

function UploadFile($u, [string]$path, [string]$filename) {
    $code = curl.exe -s -o $env:TEMP\r6_out.json -w "%{http_code}" -X POST `
        -H "Authorization: Bearer $($u.token)" -F "file=@$path;filename=$filename" "$base/api/files/upload"
    $body = [System.IO.File]::ReadAllText("$env:TEMP\r6_out.json", [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($code -ne "200") { throw "upload failed: $code $body" }
    return $body
}

# 轮询等待解析状态（最多 $timeoutSec 秒）
function WaitParseStatus($u, [int64]$fileId, [string]$expect, [int]$timeoutSec = 20) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $list = Invoke-RestMethod -Uri "$base/api/files?page=1&pageSize=50" -Headers @{ "Authorization" = "Bearer $($u.token)" }
        $item = $list.items | Where-Object { $_.id -eq $fileId }
        if ($item -and $item.parse_status -eq $expect) { return $item }
        Start-Sleep -Seconds 2
    }
    return $null
}

$u = NewUser "r6n"
$tmp = Join-Path $env:TEMP "r6n_$((Get-Date -Format 'HHmmss'))"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

# 1. txt 上传 → 自动入队 → success
$txt = Join-Path $tmp "notes.txt"
Set-Content -Path $txt -Value "R6 正常测试第一段内容，包含独特标记 QWERTY2026。`n`n第二段补充说明内容。" -Encoding UTF8
$f = UploadFile $u $txt "notes.txt"
Check "上传返回 id" ($f.id -gt 0) $f
$item = WaitParseStatus $u ([int64]$f.id) "success"
Check "txt 状态流转 success" ($null -ne $item) "timeout"
Check "列表带 parse_status 字段" ($null -ne $item.parse_status) $item

# 2. 预览闭环：内容与源文件一致
try {
    $pv = Invoke-RestMethod -Uri "$base/api/files/$($f.id)/preview" -Headers @{ "Authorization" = "Bearer $($u.token)" }
    Check "预览 previewable" ($pv.previewable -eq $true) $pv
    Check "预览含原文标记" ($pv.text -like "*QWERTY2026*") $pv.text
    Check "预览节点数" ($pv.node_count -ge 2) $pv.node_count
} catch { Check "预览接口异常" $false $_.Exception.Message }

# 3. md 上传 → 标题保留（ASCII 标记断言，避免 PS 5.1 中文解码坑）
$md = Join-Path $tmp "doc.md"
Set-Content -Path $md -Value "# MD-HEADER-001 第一章`n`n正文段落内容。" -Encoding UTF8
$f2 = UploadFile $u $md "doc.md"
$item2 = WaitParseStatus $u ([int64]$f2.id) "success"
Check "md 状态 success" ($null -ne $item2) "timeout"
try {
    $pv2 = Invoke-RestMethod -Uri "$base/api/files/$($f2.id)/preview" -Headers @{ "Authorization" = "Bearer $($u.token)" }
    Check "md 预览含标题标记" ($pv2.text -like "*MD-HEADER-001*") $pv2.text
} catch { Check "md 预览异常" $false $_.Exception.Message }

# 4. 秒传同样入队并解析
$f3 = UploadFile $u $txt "notes_copy.txt"
$item3 = WaitParseStatus $u ([int64]$f3.id) "success"
Check "秒传状态 success" ($null -ne $item3) "timeout"

# 5. reparse 幂等保护：success 文件不可重试
$code4 = curl.exe -s -o NUL -w "%{http_code}" -X POST -H "Authorization: Bearer $($u.token)" "$base/api/files/$($f.id)/reparse"
Check "success 文件 reparse 拒绝(400)" ($code4 -eq "400") $code4

Write-Host ""
if ($fail -eq 0) { Write-Host "R6 正常测试: 全部通过" } else { Write-Host "R6 正常测试: $fail 项失败" }
exit $fail
