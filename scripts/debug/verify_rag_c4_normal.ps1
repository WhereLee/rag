# C4 正常测试：上传 → 解析 → 问答闭环（检索 → SSE 流式 → 引用溯源）
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

function WaitParseStatus($u, [int64]$fileId, [string]$expect, [int]$timeoutSec = 90) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $list = Invoke-RestMethod -Uri "$base/api/files?page=1&pageSize=50" -Headers @{ "Authorization" = "Bearer $($u.token)" }
        $item = $list.items | Where-Object { $_.id -eq $fileId }
        if ($item -and $item.parse_status -eq $expect) { return $item }
        Start-Sleep -Seconds 2
    }
    return $null
}

# SSE 流式会把一个词拆进多个 delta 事件，拼接后才是完整回答文本
function JoinDeltas([string]$raw) {
    $sb = [System.Text.StringBuilder]::new()
    foreach ($m in [regex]::Matches($raw, '"type": "delta", "text": "((?:[^"\\]|\\.)*)"')) {
        $t = $m.Groups[1].Value -replace '\\n', "`n" -replace '\\"', '"' -replace '\\\\', '\\'
        [void]$sb.Append($t)
    }
    return $sb.ToString()
}

function AskSSE($u, [string]$query, [string]$outFile) {
    # curl 流式收 SSE 原始字节到文件（避免 PS 管道 GBK 重解码）
    $body = "{`"query`":`"$query`"}"
    $tmpJson = Join-Path $env:TEMP "c4_body.json"
    [System.IO.File]::WriteAllText($tmpJson, $body, (New-Object System.Text.UTF8Encoding($false)))
    $code = curl.exe -s -N -o $outFile -w "%{http_code}" -X POST `
        -H "Authorization: Bearer $($u.token)" -H "Content-Type: application/json; charset=utf-8" `
        --data-binary "@$tmpJson" "$base/api/qa/ask"
    return $code
}

$u = NewUser "c4n"
$tmp = Join-Path $env:TEMP "c4n_$((Get-Date -Format 'HHmmss'))"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

# 1. 上传带独特标记的文档（md 标题 + 检索规则内容）
$md = Join-Path $tmp "检索规则.md"
[System.IO.File]::WriteAllText($md, "# 检索规则`n`n## 切块要求`n`n检索块长度上限为 500 字，相邻块重叠 60 字。`n`n## 相关性判定`n`n向量相似度低于 0.62 不得作为答案依据，精排分数低于 -5 判为不相关。独特标记 QA_MARK_2026。", (New-Object System.Text.UTF8Encoding($false)))
$respFile = Join-Path $env:TEMP "c4_upload.json"
$code = curl.exe -s -o $respFile -w "%{http_code}" -X POST `
    -H "Authorization: Bearer $($u.token)" -F "file=@$md;filename=检索规则.md" "$base/api/files/upload"
$resp = [System.IO.File]::ReadAllText($respFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
Check "上传成功" ($code -eq "200" -and $resp.id -gt 0) $code
$item = WaitParseStatus $u ([int64]$resp.id) "success"
Check "文档解析成功" ($null -ne $item) "timeout"

# 2. 问答：真实问题 → SSE 流式 + 引用
$out = Join-Path $tmp "ask1.txt"
$code = AskSSE $u "检索块长度上限是多少" $out
Check "ask HTTP 200" ($code -eq "200") $code
$raw = [System.IO.File]::ReadAllText($out, [System.Text.Encoding]::UTF8)
Check "SSE meta 事件" ($raw -match '"type": "meta"') $raw.Substring(0, [Math]::Min(200, $raw.Length))
Check "未拒答" ($raw -match '"rejected": false') ""
Check "SSE delta 事件" ($raw -match '"type": "delta"') ""
Check "SSE done 事件" ($raw -match '"type": "done"') ""
Check "带引用来源" ($raw -match '"citations"') ""
Check "引用含文档名" ($raw -match '检索规则') ""

# 3. 流式增量：delta 事件数 ≥ 2（正文逐块输出而非一次性）
$deltaCount = ([regex]::Matches($raw, '"type": "delta"')).Count
Check "流式增量输出" ($deltaCount -ge 2) "delta=$deltaCount"

# 4. 回答内容：直接问文档标记 → 回答应引用原文（ASCII 断言绕开 PS 中文解码坑）
$out2 = Join-Path $tmp "ask2.txt"
$code2 = AskSSE $u "文档中的独特标记是什么" $out2
Check "ask2 HTTP 200" ($code2 -eq "200") $code2
$raw2 = [System.IO.File]::ReadAllText($out2, [System.Text.Encoding]::UTF8)
$text2 = JoinDeltas $raw2
Check "回答引用原文标记" ($text2 -match "QA_MARK_2026") "LLM 未直接引用标记: $text2"

Write-Host "`nC4 normal: $(if ($fail -eq 0) { 'PASS' } else { "$fail FAILED" })"
exit $(if ($fail -eq 0) { 0 } else { 1 })
