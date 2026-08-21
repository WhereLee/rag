# 阶段 2 正常测试：秒传命中 / 引用计数 / 回收站恢复 / 同名标记 / 回归
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$projectRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$stamp = Get-Date -Format "HHmmss"
$ua = "s2_n_a_$stamp"; $ub = "s2_n_b_$stamp"
$passwd = "Passw0rd1"
$script:fail = 0
$xffA = @{ "X-Forwarded-For" = "203.0.113.51" }
$xffB = @{ "X-Forwarded-For" = "203.0.113.52" }

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

function Sql($sql) {
    $env:PGCLIENTENCODING = "UTF8"
    $dsn = (Get-Content (Join-Path $projectRoot ".env") | Where-Object { $_ -match '^PG_DSN=' } | Select-Object -First 1) -replace '^PG_DSN=', ''
    & psql $dsn -t -c $sql 2>$null
}

# 注册 A/B + 登录
PostJson "$base/api/auth/register" @{ username = $ua; password = $passwd } $xffA | Out-Null
PostJson "$base/api/auth/register" @{ username = $ub; password = $passwd } $xffB | Out-Null
$loginA = PostJson "$base/api/auth/login" @{ username = $ua; password = $passwd }
$tokA = $loginA.token
$tokB = (PostJson "$base/api/auth/login" @{ username = $ub; password = $passwd }).token
$headersA = @{ Authorization = "Bearer $tokA" }
$headersB = @{ Authorization = "Bearer $tokB" }
Check "注册+登录 A/B" ($tokA -ne "" -and $tokB -ne "") "token empty"

# A 上传一个文件 + 同名上传标记（同名但内容相同 → 秒传去重 + duplicate_name）
$srcFile = Join-Path $projectRoot "data\corpus\tech\mineru_readme.md"
$hash = (Get-FileHash $srcFile -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $srcFile).Length
# 清理该 hash 的历史残留（测试内容固定 → hash 固定 → 重跑时保证环境干净）
Sql "DELETE FROM user_file WHERE blob_id IN (SELECT id FROM file_blob WHERE file_hash='$hash')" | Out-Null
Sql "DELETE FROM file_blob WHERE file_hash='$hash'" | Out-Null
$upA = Upload $tokA $srcFile "mineru.md"
Check "A 上传文件" ($upA.id -ne $null) "id missing"
Check "A 首次上传无 duplicate_name" ($null -eq $upA.duplicate_name -or $upA.duplicate_name -eq $false) "dup=$($upA.duplicate_name)"
$upA2 = Upload $tokA $srcFile "mineru.md"
Check "同名上传带 duplicate_name" ($upA2.duplicate_name -eq $true) "dup=$($upA2.duplicate_name)"

# B 秒传同一内容（不同文件名）→ 命中，不传字节
$chk = PostJson "$base/api/files/check-hash" @{ hash = $hash; size = $size; filename = "mineru_copy.md" } $headersB
Check "B 秒传命中" ($chk.hit -eq $true -and $chk.id -ne $null) "resp=$($chk | ConvertTo-Json)"
Check "B 秒传响应带 id" ($chk.id -gt 0) "id=$($chk.id)"

# blob 引用计数 = 3（A 两份 + B）
$refCount = Sql "SELECT ref_count FROM file_blob WHERE file_hash='$hash'"
Check "blob ref_count=3" ($refCount.Trim() -eq "3") "ref=$refCount"

# A 删除（进回收站）→ ref 2 → 物理文件仍在（B 还引用）
Invoke-RestMethod -Uri "$base/api/files/$($upA.id)" -Method Delete -Headers $headersA | Out-Null
$refCount = Sql "SELECT ref_count FROM file_blob WHERE file_hash='$hash'"
Check "A 删除后 ref_count=2" ($refCount.Trim() -eq "2") "ref=$refCount"

# B 下载（字节一致，证明共享物理文件）
Invoke-WebRequest -Uri "$base/api/files/$($chk.id)/download" -Headers $headersB -OutFile "$env:TEMP\s2_dl.md" | Out-Null
Check "B 下载共享文件字节一致" ((Get-Item "$env:TEMP\s2_dl.md").Length -eq $size) "len mismatch"

# A 回收站有记录 → 恢复 → 下载正常
$trashA = Invoke-RestMethod -Uri "$base/api/files/trash" -Headers $headersA
Check "A 回收站有记录" ($trashA.Count -eq 1 -and $trashA[0].id -eq $upA.id) "count=$($trashA.Count)"
Invoke-RestMethod -Uri "$base/api/files/$($upA.id)/restore" -Method Post -Headers $headersA | Out-Null
$listA = Invoke-RestMethod -Uri "$base/api/files" -Headers $headersA
Check "恢复后回到列表（含同名一份）" ($listA.total -eq 2) "total=$($listA.total)"
Invoke-WebRequest -Uri "$base/api/files/$($upA.id)/download" -Headers $headersA -OutFile "$env:TEMP\s2_dl2.md" | Out-Null
Check "恢复后下载正常" ((Get-Item "$env:TEMP\s2_dl2.md").Length -eq $size) "len mismatch"

# B 删除（A 已恢复+同名，仍引用）→ ref 2；A 逐份删除 → 归零删 blob
Invoke-RestMethod -Uri "$base/api/files/$($chk.id)" -Method Delete -Headers $headersB | Out-Null
$refCount = Sql "SELECT ref_count FROM file_blob WHERE file_hash='$hash'"
Check "B 删除后 ref_count=2（A 仍引用）" ($refCount.Trim() -eq "2") "ref=$refCount"
Invoke-RestMethod -Uri "$base/api/files/$($upA.id)" -Method Delete -Headers $headersA | Out-Null
$refCount = Sql "SELECT ref_count FROM file_blob WHERE file_hash='$hash'"
Check "A 删一份后 ref_count=1" ($refCount.Trim() -eq "1") "ref=$refCount"
Invoke-RestMethod -Uri "$base/api/files/$($upA2.id)" -Method Delete -Headers $headersA | Out-Null
$refCount = Sql "SELECT ref_count FROM file_blob WHERE file_hash='$hash'"
Check "最后引用删除后 blob 记录消失" ($refCount.Trim() -eq "") "ref=$refCount"
$blobRows = Sql "SELECT count(*) FROM file_blob WHERE file_hash='$hash'"
Check "引用归零后 blob 记录已删" ($blobRows.Trim() -eq "0") "rows=$blobRows"

# 回归：清理接口 / 分页（A 文件已全删，先传一个再验证分页）
$cl = Invoke-RestMethod -Uri "$base/api/files/cleanup" -Method Post -Headers $headersA
Check "清理接口回归" ($null -ne $cl.trash_cleaned -and $null -ne $cl.blob_cleaned) "resp=$($cl | ConvertTo-Json)"
Upload $tokA $srcFile "regression.md" | Out-Null
$r = Invoke-RestMethod -Uri "$base/api/files?page=1&pageSize=1" -Headers $headersA
Check "分页回归" ($r.items.Count -eq 1 -and $r.total -eq 1) "items=$($r.items.Count) total=$($r.total)"

Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 阶段2 正常测试全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
