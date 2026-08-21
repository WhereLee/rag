# 第一步验收：登录/上传/管理文件 + 双账号隔离
# 用法：powershell -File scripts\debug\verify_files_step1.ps1
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$projectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$testFile = Join-Path $projectRoot "data\corpus\tech\fastapi_readme.md"

$u1 = "user_a_" + (Get-Date -Format "HHmmss")
$u2 = "user_b_" + (Get-Date -Format "HHmmss")

function PostJson($url, $obj) {
    Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Body ($obj | ConvertTo-Json)
}

function HttpStatus($scriptBlock) {
    try { & $scriptBlock | Out-Null; return 200 } catch { return $_.Exception.Response.StatusCode.value__ }
}

# 1. 注册 A、B
PostJson "$base/api/auth/register" @{username = $u1; password = "pass123456"} | Out-Null
PostJson "$base/api/auth/register" @{username = $u2; password = "pass123456"} | Out-Null
Write-Host "[OK] 注册 A=$u1 B=$u2"

# 2. 登录拿 token
$tokA = (PostJson "$base/api/auth/login" @{username = $u1; password = "pass123456"}).token
$tokB = (PostJson "$base/api/auth/login" @{username = $u2; password = "pass123456"}).token
$headersA = @{ Authorization = "Bearer $tokA" }
$headersB = @{ Authorization = "Bearer $tokB" }
Write-Host "[OK] A/B 登录成功"

# 3. A 上传文件
$upRaw = curl.exe -s -X POST -H "Authorization: Bearer $tokA" -F "file=@$testFile;filename=fastapi_readme.md" "$base/api/files/upload"
$up = $upRaw | ConvertFrom-Json
if (-not $up.id) { throw "上传失败: $upRaw" }
$fileId = $up.id
Write-Host "[OK] A 上传成功 fileId=$fileId name=$($up.filename)"

# 4. A 列表：1 条
$listA = Invoke-RestMethod -Uri "$base/api/files" -Headers $headersA
if ($listA.Count -ne 1) { throw "A 列表应为 1 条，实际 $($listA.Count)" }
Write-Host "[OK] A 列表 1 条"

# 5. B 列表：空（隔离）
$listB = Invoke-RestMethod -Uri "$base/api/files" -Headers $headersB
if ($listB.Count -ne 0) { throw "B 列表应为空，实际 $($listB.Count)" }
Write-Host "[OK] B 列表为空（隔离）"

# 6. B 删除 A 的文件 -> 404（不泄露存在性）
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files/$fileId" -Method Delete -Headers $headersB }
if ($st -ne 404) { throw "B 删除 A 文件应 404，实际 $st" }
Write-Host "[OK] B 删除 A 的文件 -> 404（隔离）"

# 7. B 下载 A 的文件 -> 404
$st = HttpStatus { Invoke-WebRequest -Uri "$base/api/files/$fileId/download" -Headers $headersB -OutFile "$env:TEMP\b_dl.bin" }
if ($st -ne 404) { throw "B 下载 A 文件应 404，实际 $st" }
Write-Host "[OK] B 下载 A 的文件 -> 404（隔离）"

# 8. A 下载自己的文件：字节一致
Invoke-WebRequest -Uri "$base/api/files/$fileId/download" -Headers $headersA -OutFile "$env:TEMP\a_dl.md" | Out-Null
$origLen = (Get-Item $testFile).Length
$dlLen = (Get-Item "$env:TEMP\a_dl.md").Length
if ($origLen -ne $dlLen) { throw "下载字节不一致 $origLen vs $dlLen" }
Write-Host "[OK] A 下载自己的文件，字节一致（$dlLen B）"

# 9. A 删除自己的文件 -> 列表清空
Invoke-RestMethod -Uri "$base/api/files/$fileId" -Method Delete -Headers $headersA | Out-Null
$listA2 = Invoke-RestMethod -Uri "$base/api/files" -Headers $headersA
if ($listA2.Count -ne 0) { throw "A 删除后列表应为空" }
Write-Host "[OK] A 删除成功，列表清空"

# 10. 磁盘文件已清理
$stored = Get-ChildItem -Path (Join-Path $projectRoot "data\files") -Recurse -ErrorAction SilentlyContinue
$leftover = $stored | Where-Object { -not $_.PSIsContainer }
if ($leftover) { Write-Host "[WARN] 磁盘残留: $($leftover.FullName -join ', ')" }

Write-Host ""
Write-Host "===== 全部验收通过 ====="
