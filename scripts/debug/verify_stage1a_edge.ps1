# 阶段 1a 边界测试：分页参数边界 + 重命名异常（越权/空名/超长/同名/路径字符/不存在）
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$projectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$stamp = Get-Date -Format "HHmmss"
$ua = "s1a_e_a_$stamp"; $ub = "s1a_e_b_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xffA = @{ "X-Forwarded-For" = "203.0.113.21" }
$xffB = @{ "X-Forwarded-For" = "203.0.113.22" }

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host "[OK] $name" } else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

function PostJson($url, $obj, $headers = @{}) {
    Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Headers $headers -Body ($obj | ConvertTo-Json)
}

function HttpStatus($scriptBlock) {
    try { & $scriptBlock | Out-Null; return 200 } catch { return $_.Exception.Response.StatusCode.value__ }
}

function Upload($token, $content, $filename) {
    $tmp = Join-Path $env:TEMP $filename
    Set-Content -Path $tmp -Value $content -Encoding UTF8
    $r = curl.exe -s -X POST -H "Authorization: Bearer $token" -F "file=@$tmp;filename=$filename" "$base/api/files/upload"
    Remove-Item $tmp -ErrorAction SilentlyContinue
    $r | ConvertFrom-Json
}

function TryRename($token, $id, $name) {
    $body = @{ filename = $name } | ConvertTo-Json
    HttpStatus { Invoke-RestMethod -Uri "$base/api/files/$id/rename" -Method Put -ContentType "application/json" -Headers @{ Authorization = "Bearer $token" } -Body $body }
}

# 注册 A、B + 登录
PostJson "$base/api/auth/register" @{ username = $ua; password = $passwd } $xffA | Out-Null
PostJson "$base/api/auth/register" @{ username = $ub; password = $passwd } $xffB | Out-Null
$tokA = (PostJson "$base/api/auth/login" @{ username = $ua; password = $passwd }).token
$tokB = (PostJson "$base/api/auth/login" @{ username = $ub; password = $passwd }).token
$headersA = @{ Authorization = "Bearer $tokA" }
$headersB = @{ Authorization = "Bearer $tokB" }
Check "注册+登录 A/B" ($tokA -ne "" -and $tokB -ne "") "token empty"

# A 上传 1 个文件，B 上传 2 个文件
$fileA = Upload $tokA "hello A content" "a.txt"
$fileB1 = Upload $tokB "hello B one" "b1.txt"
$fileB2 = Upload $tokB "hello B two" "b2.txt"
Check "A/B 上传就绪" ($fileA.id -and $fileB1.id -and $fileB2.id) "ids missing"
$idA = $fileA.id; $idB1 = $fileB1.id

# ---- 分页参数边界 ----
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files?page=0" -Headers $headersA }
Check "page=0 -> 400" ($st -eq 400) "status=$st"
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files?page=-5" -Headers $headersA }
Check "page=-5 -> 400" ($st -eq 400) "status=$st"
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files?pageSize=0" -Headers $headersA }
Check "pageSize=0 -> 400" ($st -eq 400) "status=$st"
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files?pageSize=101" -Headers $headersA }
Check "pageSize=101 -> 400" ($st -eq 400) "status=$st"
$r = Invoke-RestMethod -Uri "$base/api/files?pageSize=100" -Headers $headersA
Check "pageSize=100 合法" ($r.items.Count -eq 1 -and $r.total -eq 1) "items=$($r.items.Count)"
$r = Invoke-RestMethod -Uri "$base/api/files?page=999" -Headers $headersA
Check "page=999 -> 空列表" ($r.items.Count -eq 0 -and $r.total -eq 1) "items=$($r.items.Count)"

# ---- 重命名边界 ----
$st = TryRename $tokB $idA "hack.txt"
Check "B 重命名 A 的文件 -> 404" ($st -eq 404) "status=$st"
$st = TryRename $tokB 999999 "x.txt"
Check "重命名不存在的文件 -> 404" ($st -eq 404) "status=$st"
$st = TryRename $tokB $idB1 "   "
Check "重命名为空 -> 400" ($st -eq 400) "status=$st"
$longName = "n" * 256 + ".txt"
$st = TryRename $tokB $idB1 $longName
Check "重命名超长(256) -> 400" ($st -eq 400) "status=$st"
$st = TryRename $tokB $idB1 "b2.txt"
Check "重命名成同名 -> 400" ($st -eq 400) "status=$st"

# 路径字符：sanitizeFilename 取路径最后一段（防穿越第一道防线，比替换下划线更严格）
$st = TryRename $tokB $idB1 "a/b.txt"
Check "含路径字符 -> 200 且安全处理" ($st -eq 200) "status=$st"
$r = Invoke-RestMethod -Uri "$base/api/files?page=1&pageSize=20" -Headers $headersB
$renamed = $r.items | Where-Object { $_.id -eq $idB1 }
Check "路径字符被截断为 b.txt（防穿越）" ($renamed.filename -eq "b.txt") "name=$($renamed.filename)"

# 未登录访问 -> 401
$st = HttpStatus { Invoke-RestMethod -Uri "$base/api/files" }
Check "无 token 列表 -> 401" ($st -eq 401) "status=$st"

Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段1a 边界测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
