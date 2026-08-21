# 阶段 1a 正常测试：友好类型展示 + 列表分页 + 重命名（主流程）
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$projectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$stamp = Get-Date -Format "HHmmss"
$u = "s1a_n_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xff = @{ "X-Forwarded-For" = "203.0.113.11" }

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host "[OK] $name" } else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

function PostJson($url, $obj, $headers = @{}) {
    Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Headers $headers -Body ($obj | ConvertTo-Json)
}

function Upload($token, $path, $filename) {
    $r = curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$path;filename=$filename" "$base/api/files/upload"
    $r | ConvertFrom-Json
}

# 注册 + 登录（假 IP 避开注册限流）
PostJson "$base/api/auth/register" @{ username = $u; password = $passwd } $xff | Out-Null
$token = (PostJson "$base/api/auth/login" @{ username = $u; password = $passwd }).token
$headers = @{ Authorization = "Bearer $token" }
Check "注册+登录" ($token -ne "") "token empty"

# 上传 3 个文件（2 md + 1 pdf，pdf 最后传 → 列表第一项）
$f1 = Join-Path $projectRoot "data\corpus\tech\fastapi_readme.md"
$f2 = Join-Path $projectRoot "data\corpus\tech\mineru_readme.md"
$f3 = Join-Path $projectRoot "data\corpus\whitepaper\rag_survey_arxiv.pdf"
$up1 = Upload $token $f1 "readme_a.md"
$up2 = Upload $token $f2 "readme_b.md"
$up3 = Upload $token $f3 "survey.pdf"
Check "上传 3 个文件" ($up1.id -and $up2.id -and $up3.id) "ids=$($up1.id),$($up2.id),$($up3.id)"
$pdfId = $up3.id

# 列表分页：page=1 pageSize=2 → 2 条 + total=3
$r = Invoke-RestMethod -Uri "$base/api/files?page=1&pageSize=2" -Headers $headers
Check "分页 page=1 返回结构" ($r.items.Count -eq 2 -and $r.total -eq 3 -and $r.page -eq 1 -and $r.pageSize -eq 2) "items=$($r.items.Count) total=$($r.total)"
Check "ext 字段正确（pdf）" ($r.items[0].ext -eq ".pdf") "ext=$($r.items[0].ext)"
Check "ext 字段正确（md）" ($r.items[1].ext -eq ".md") "ext=$($r.items[1].ext)"

# 列表分页：page=2 pageSize=2 → 1 条
$r2 = Invoke-RestMethod -Uri "$base/api/files?page=2&pageSize=2" -Headers $headers
Check "分页 page=2 剩 1 条" ($r2.items.Count -eq 1 -and $r2.total -eq 3) "items=$($r2.items.Count)"

# 重命名 pdf 文件
$body = @{ filename = "survey_renamed.pdf" } | ConvertTo-Json
$rr = Invoke-RestMethod -Uri "$base/api/files/$pdfId/rename" -Method Put -ContentType "application/json" -Headers $headers -Body $body
Check "重命名成功" ($rr.renamed -eq $true) "resp=$($rr | ConvertTo-Json)"

# 列表验证新名 + ext 仍为 .pdf
$r3 = Invoke-RestMethod -Uri "$base/api/files?page=1&pageSize=1" -Headers $headers
Check "列表显示新文件名" ($r3.items[0].filename -eq "survey_renamed.pdf") "name=$($r3.items[0].filename)"
Check "重命名后 ext 保持" ($r3.items[0].ext -eq ".pdf") "ext=$($r3.items[0].ext)"

# 下载重命名后的文件：字节一致
Invoke-WebRequest -Uri "$base/api/files/$pdfId/download" -Headers $headers -OutFile "$env:TEMP\s1a_dl.pdf" | Out-Null
$origLen = (Get-Item $f3).Length
$dlLen = (Get-Item "$env:TEMP\s1a_dl.pdf").Length
Check "下载字节一致" ($origLen -eq $dlLen) "$origLen vs $dlLen"

Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段1a 正常测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
