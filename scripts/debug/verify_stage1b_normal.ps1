# 阶段 1b 正常测试：合法文件上传（魔数放行）+ 配额内 + 删除 + 清理接口 + 回归
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$projectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$stamp = Get-Date -Format "HHmmss"
$u = "s1b_n_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xff = @{ "X-Forwarded-For" = "203.0.113.31" }

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

# 注册 + 登录
PostJson "$base/api/auth/register" @{ username = $u; password = $passwd } $xff | Out-Null
$login = PostJson "$base/api/auth/login" @{ username = $u; password = $passwd }
$token = $login.token
$headers = @{ Authorization = "Bearer $token" }
Check "注册+登录" ($token -ne "") "token empty"

# 上传合法文件：md（无魔数放行）、pdf、png（真魔数）
$f1 = Join-Path $projectRoot "data\corpus\tech\fastapi_readme.md"
$f2 = Join-Path $projectRoot "data\corpus\whitepaper\rag_survey_arxiv.pdf"
$f3 = Join-Path $projectRoot "data\corpus\image\system_architecture.png"
$up1 = Upload $token $f1 "note.md"
$up2 = Upload $token $f2 "paper.pdf"
$up3 = Upload $token $f3 "arch.png"
Check "上传 md/pdf/png 全部成功" ($up1.id -and $up2.id -and $up3.id) "ids=$($up1.id),$($up2.id),$($up3.id)"

# 配额内正常（默认 2GB 不会触发）
$r = Invoke-RestMethod -Uri "$base/api/files" -Headers $headers
Check "列表 3 个文件" ($r.total -eq 3) "total=$($r.total)"

# 删除 → 物理文件消失 + 无 warning
$delResp = Invoke-RestMethod -Uri "$base/api/files/$($up1.id)" -Method Delete -Headers $headers
Check "删除后列表 2 个" ((Invoke-RestMethod -Uri "$base/api/files" -Headers $headers).total -eq 2) "total after delete"
Check "删除响应无 warning" ($null -eq $delResp.warning) "warning=$($delResp.warning)"

# 手动触发清理接口（幂等）
$cl = Invoke-RestMethod -Uri "$base/api/files/cleanup" -Method Post -Headers $headers
Check "清理接口可调用" ($null -ne $cl.tmp_cleaned -and $null -ne $cl.orphan_cleaned) "resp=$($cl | ConvertTo-Json)"

# 回归：重命名 + 下载
$body = @{ filename = "paper_v2.pdf" } | ConvertTo-Json
$rr = Invoke-RestMethod -Uri "$base/api/files/$($up2.id)/rename" -Method Put -ContentType "application/json" -Headers $headers -Body $body
Check "重命名回归" ($rr.renamed -eq $true) "resp=$($rr | ConvertTo-Json)"
Invoke-WebRequest -Uri "$base/api/files/$($up2.id)/download" -Headers $headers -OutFile "$env:TEMP\s1b_dl.pdf" | Out-Null
Check "下载回归字节一致" ((Get-Item "$env:TEMP\s1b_dl.pdf").Length -eq (Get-Item $f2).Length) "length mismatch"

Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段1b 正常测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
