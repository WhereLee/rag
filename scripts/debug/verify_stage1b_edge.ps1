# 阶段 1b 边界测试：伪装类型 / 配额边界 / 上传限流 / 孤儿清理 / 越权删除
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$projectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$stamp = Get-Date -Format "HHmmss"
$ua = "s1b_e_a_$stamp"; $ub = "s1b_e_b_$stamp"; $uc = "s1b_e_c_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xffA = @{ "X-Forwarded-For" = "203.0.113.41" }
$xffB = @{ "X-Forwarded-For" = "203.0.113.42" }
$xffC = @{ "X-Forwarded-For" = "203.0.113.43" }
$QUOTA = 2147483648

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host "[OK] $name" } else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

function PostJson($url, $obj, $headers = @{}) {
    Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Headers $headers -Body ($obj | ConvertTo-Json)
}

function HttpStatus($scriptBlock) {
    try { & $scriptBlock | Out-Null; return 200 } catch { return $_.Exception.Response.StatusCode.value__ }
}

function UploadBytes($token, $bytes, $filename) {
    $tmp = Join-Path $env:TEMP ("s1b_" + [guid]::NewGuid().ToString("N") + ".bin")
    [System.IO.File]::WriteAllBytes($tmp, $bytes)
    $r = curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$tmp;filename=$filename" "$base/api/files/upload"
    Remove-Item $tmp -ErrorAction SilentlyContinue
    $r | ConvertFrom-Json
}

function SqlExec($sql) {
    $env:PGCLIENTENCODING = "UTF8"
    $dsn = (Get-Content (Join-Path $projectRoot ".env") | Where-Object { $_ -match '^PG_DSN=' } | Select-Object -First 1) -replace '^PG_DSN=', ''
    & psql $dsn -t -c $sql 2>$null
}

# 注册 A/B/C + 登录
PostJson "$base/api/auth/register" @{ username = $ua; password = $passwd } $xffA | Out-Null
PostJson "$base/api/auth/register" @{ username = $ub; password = $passwd } $xffB | Out-Null
PostJson "$base/api/auth/register" @{ username = $uc; password = $passwd } $xffC | Out-Null
$tokA = (PostJson "$base/api/auth/login" @{ username = $ua; password = $passwd }).token
$tokB = (PostJson "$base/api/auth/login" @{ username = $ub; password = $passwd }).token
$tokC = (PostJson "$base/api/auth/login" @{ username = $uc; password = $passwd }).token
$headersA = @{ Authorization = "Bearer $tokA" }
$headersB = @{ Authorization = "Bearer $tokB" }
$headersC = @{ Authorization = "Bearer $tokC" }
Check "注册+登录 A/B/C" ($tokA -ne "" -and $tokB -ne "" -and $tokC -ne "") "token empty"

# A 上传 1 个合法文件（供配额/删除测试）
$legit = UploadBytes $tokA ([System.Text.Encoding]::UTF8.GetBytes("hello quota base")) "base.txt"
Check "A 上传基准文件" ($legit.id -ne $null) "id missing"
$idA = $legit.id
$sizeA = $legit.file_size

# ---- 伪装类型（断言不依赖中文比较：curl 输出经 PS5.1 管道会乱码，只看 error 字段存在性）----
# exe 内容（MZ 头）声明 .pdf
$exeBytes = [System.Text.Encoding]::ASCII.GetBytes("MZ" + "fake exe content padding")
$r = UploadBytes $tokA $exeBytes "evil.pdf"
Check "exe->pdf 拒绝 400" ($null -eq $r.id -and $null -ne $r.error) "resp=$($r | ConvertTo-Json)"
# 文本内容声明 .png
$r = UploadBytes $tokA ([System.Text.Encoding]::UTF8.GetBytes("plain text pretending png")) "fake.png"
Check "txt->png 拒绝 400" ($null -eq $r.id -and $null -ne $r.error) "resp=$($r | ConvertTo-Json)"
# 真 PDF 内容声明 .png（真文件但类型不匹配）
$pdfBytes = [System.IO.File]::ReadAllBytes((Join-Path $projectRoot "data\corpus\whitepaper\rag_survey_arxiv.pdf"))
$r = UploadBytes $tokA $pdfBytes "paper.png"
Check "pdf->png 拒绝 400" ($null -eq $r.id -and $null -ne $r.error) "resp=$($r | ConvertTo-Json)"

# ---- 配额边界：占用 QUOTA-1，上传 10 字节 -> 超 9 -> 拒绝；恢复 -> 成功 ----
SqlExec "UPDATE user_file SET file_size=$($QUOTA - 1) WHERE id=$idA" | Out-Null
$r = UploadBytes $tokA ([System.Text.Encoding]::UTF8.GetBytes("over quota")) "over.txt"
Check "配额超 9 字节上传被拒" ($null -eq $r.id -and $null -ne $r.error) "resp=$($r | ConvertTo-Json)"
SqlExec "UPDATE user_file SET file_size=$sizeA WHERE id=$idA" | Out-Null
$r = UploadBytes $tokA ([System.Text.Encoding]::UTF8.GetBytes("fits now")) "fits.txt"
Check "配额恢复后上传成功" ($r.id -ne $null) "resp=$($r | ConvertTo-Json)"

# ---- 上传限流：C 账号连续 11 次，第 11 次 429 ----
$okCount = 0; $tooMany = $false
for ($i = 1; $i -le 11; $i++) {
    $resp = UploadBytes $tokC ([System.Text.Encoding]::UTF8.GetBytes("chunk $i")) "c$i.txt"
    if ($resp.id) { $okCount++ } elseif ($null -ne $resp.error) { $tooMany = $true } else { Write-Host "unexpected: $($resp | ConvertTo-Json)" }
}
Check "限流：前 10 次成功" ($okCount -eq 10) "ok=$okCount"
Check "限流：第 11 次 429" ($tooMany -eq $true) "not limited"

# ---- 孤儿清理 ----
$userIdA = (PostJson "$base/api/auth/login" @{ username = $ua; password = $passwd }).userId
$userDir = Join-Path $projectRoot "data\files\$userIdA"
$ghostTmp = Join-Path $userDir "ghost_upload.tmp"
$ghostOrphan = Join-Path $userDir "ghost_orphan.dat"
Set-Content -Path $ghostTmp -Value "orphan tmp" -Encoding UTF8
Set-Content -Path $ghostOrphan -Value "orphan file" -Encoding UTF8
(Get-Item $ghostTmp).LastWriteTime = (Get-Date).AddHours(-2)
(Get-Item $ghostOrphan).LastWriteTime = (Get-Date).AddHours(-2)
$cl = Invoke-RestMethod -Uri "$base/api/files/cleanup" -Method Post -Headers $headersA
Check "清理任务删除过期 tmp" ($cl.tmp_cleaned -ge 1 -and -not (Test-Path $ghostTmp)) "tmp_cleaned=$($cl.tmp_cleaned) exists=$(Test-Path $ghostTmp)"
Check "清理任务删除无记录孤儿" ($cl.orphan_cleaned -ge 1 -and -not (Test-Path $ghostOrphan)) "orphan_cleaned=$($cl.orphan_cleaned) exists=$(Test-Path $ghostOrphan)"

# 新鲜的 tmp（TTL 内）不删
$freshTmp = Join-Path $userDir "fresh_upload.tmp"
Set-Content -Path $freshTmp -Value "fresh" -Encoding UTF8
$cl2 = Invoke-RestMethod -Uri "$base/api/files/cleanup" -Method Post -Headers $headersA
Check "TTL 内 tmp 保留" ($cl2.tmp_cleaned -eq 0 -and (Test-Path $freshTmp)) "cleaned=$($cl2.tmp_cleaned)"
Remove-Item $freshTmp -ErrorAction SilentlyContinue

# ---- 越权删除（回归）----
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files/$idA" -Method Delete -Headers $headersB }
Check "B 删除 A 的文件 -> 404" ($st -eq 404) "status=$st"

Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段1b 边界测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
