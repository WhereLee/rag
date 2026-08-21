# C4 边界测试：未登录 401 / 空查询 400 / 空库拒答 / 用户隔离 / 后端中断降级
# 前置：网关(8082) + parse worker + qa 服务(8091) 已启动
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
    return @{ user = $u; token = $tok }
}

function WaitParseStatus($u, [int64]$fileId, [int]$timeoutSec = 90) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $list = Invoke-RestMethod -Uri "$base/api/files?page=1&pageSize=50" -Headers @{ "Authorization" = "Bearer $($u.token)" }
        $item = $list.items | Where-Object { $_.id -eq $fileId }
        if ($item -and $item.parse_status -eq "success") { return $item }
        Start-Sleep -Seconds 2
    }
    return $null
}

function AskSSE($u, [string]$query, [string]$outFile, [string]$token = "") {
    $body = "{`"query`":`"$query`"}"
    $tmpJson = Join-Path $env:TEMP "c4e_body.json"
    [System.IO.File]::WriteAllText($tmpJson, $body, (New-Object System.Text.UTF8Encoding($false)))
    $auth = if ($token) { @("-H", "Authorization: Bearer $token") } else { @() }
    $code = curl.exe -s -N -o $outFile -w "%{http_code}" -X POST `
        -H "Content-Type: application/json; charset=utf-8" `
        @auth --data-binary "@$tmpJson" "$base/api/qa/ask"
    return $code
}

$tmp = Join-Path $env:TEMP "c4e_$((Get-Date -Format 'HHmmss'))"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

# 1. 未登录 401
$out = Join-Path $tmp "noauth.txt"
$code = AskSSE $null "任意问题" $out ""
Check "未登录 401" ($code -eq "401") $code

# 2. 空查询 400（登录用户）
$u = NewUser "c4e"
$out = Join-Path $tmp "empty.txt"
$code = AskSSE $u "" $out $u.token
Check "空查询 400" ($code -eq "400") $code

# 3. 空库拒答：新用户无文档 → meta rejected=true
$out = Join-Path $tmp "reject.txt"
$code = AskSSE $u "检索块长度上限是多少" $out $u.token
Check "空库 ask 200" ($code -eq "200") $code
$raw = [System.IO.File]::ReadAllText($out, [System.Text.Encoding]::UTF8)
Check "空库拒答 meta" ($raw -match '"rejected": true') ""
Check "拒答无 delta" (-not ($raw -match '"type": "delta"')) ""
Check "拒答含 done" ($raw -match '"type": "done"') ""

# 4. 用户隔离：A 上传文档，B 问 A 文档内容 → B 拒答（检索隔离）
$md = Join-Path $tmp "秘密.md"
[System.IO.File]::WriteAllText($md, "# 机密文档`n`n本文件仅供上传者检索，包含机密标记 SECRET_C4_7788。", (New-Object System.Text.UTF8Encoding($false)))
$respFile = Join-Path $env:TEMP "c4e_upload.json"
$code = curl.exe -s -o $respFile -w "%{http_code}" -X POST `
    -H "Authorization: Bearer $($u.token)" -F "file=@$md;filename=秘密.md" "$base/api/files/upload"
$resp = [System.IO.File]::ReadAllText($respFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$item = WaitParseStatus $u ([int64]$resp.id)
Check "A 文档解析成功" ($null -ne $item) "timeout"

$b = NewUser "c4eB"
$outB = Join-Path $tmp "isolate.txt"
$code = AskSSE $b "SECRET_C4_7788 是什么标记" $outB $b.token
Check "B ask 200" ($code -eq "200") $code
$rawB = [System.IO.File]::ReadAllText($outB, [System.Text.Encoding]::UTF8)
Check "B 检索隔离拒答" ($rawB -match '"rejected": true') "B 不应搜到 A 的文档"
Check "B 响应无机密标记" (-not ($rawB -match "SECRET_C4_7788")) "泄露！"

# 5. 后端中断：停 qa 服务 → error 事件 → 重启
$qaProc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match '8091' }
Check "qa 服务在运行" ($null -ne $qaProc) "qa 未运行"
if ($qaProc) {
    $qaProc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 2
    $outDown = Join-Path $tmp "down.txt"
    $code = AskSSE $u "检索块长度上限是多少" $outDown $u.token
    $rawDown = [System.IO.File]::ReadAllText($outDown, [System.Text.Encoding]::UTF8)
    Check "后端中断仍 200" ($code -eq "200") $code
    Check "后端中断 error 事件" ($rawDown -match '"type":"error"') $rawDown
    # 重启 qa 服务
    $python = (Get-Command python).Source
    # qa 包位于 rag-python/src/qa，工作目录必须指向 src，否则 uvicorn 找不到模块
    Start-Process -FilePath $python -ArgumentList @("-m", "uvicorn", "qa.app:app", "--host", "127.0.0.1", "--port", "8091") `
        -WorkingDirectory "c:\Users\lrs\Desktop\py\rag\rag-python\src" -WindowStyle Hidden
    Start-Sleep -Seconds 8
    $outRec = Join-Path $tmp "recover.txt"
    $code = AskSSE $u "检索块长度上限是多少" $outRec $u.token
    $rawRec = [System.IO.File]::ReadAllText($outRec, [System.Text.Encoding]::UTF8)
    Check "qa 恢复后正常" ($code -eq "200" -and $rawRec -match '"type": "meta"') "code=$code"
}

Write-Host "`nC4 edge: $(if ($fail -eq 0) { 'PASS' } else { "$fail FAILED" })"
exit $(if ($fail -eq 0) { 0 } else { 1 })
