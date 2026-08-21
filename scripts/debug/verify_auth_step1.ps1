# 认证验收：登录/注册错误提示 + 防枚举 + 并发注册兜底 + 密码策略 + 账号锁定
# 注意：注册接口每 IP 每分钟限 3 次，注册类用例之间必须间隔 >=21 秒
# 用法：powershell -File scripts\debug\verify_auth_step1.ps1
$ErrorActionPreference = "Stop"
$base = "http://localhost:8082"
$stamp = Get-Date -Format "HHmmss"
$ua = "auth_a_$stamp"; $ub = "auth_b_$stamp"
$ud = "lock_d_$stamp"
$PWD_OK = "Passw0rd1"
$script:fail = 0

function TryPost($url, $obj, [ref]$status, [ref]$body, $headers = @{}) {
    try {
        $r = Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Headers $headers -Body ($obj | ConvertTo-Json)
        $status.Value = 200; $body.Value = $r; return $true
    } catch {
        $status.Value = $_.Exception.Response.StatusCode.value__
        try { $body.Value = $_.ErrorDetails.Message | ConvertFrom-Json } catch { $body.Value = $null }
        return $false
    }
}

function Check($name, $cond, $detail) {
    if ($cond) { Write-Host "[OK] $name" } else { Write-Host "[FAIL] $name -> $detail"; $script:fail++ }
}

# ---- 注册类（间隔 21s 避开 IP 限流） ----

# 1. 注册合法账号 A
$st = 0; $bd = $null
TryPost "$base/api/auth/register" @{username = $ua; password = $PWD_OK} ([ref]$st) ([ref]$bd)
Check "注册合法账号A -> 200" ($st -eq 200) "status=$st"
Start-Sleep -Seconds 21

# 2. 注册非法用户名（中文）-> 400 明确提示
$st = 0; $bd = $null
TryPost "$base/api/auth/register" @{username = "中文名"; password = $PWD_OK} ([ref]$st) ([ref]$bd)
Check "注册非法用户名(中文) -> 400 提示" ($st -eq 400 -and $bd.error -like "*用户名*") "status=$st error=$($bd.error)"
Start-Sleep -Seconds 21

# 3. 注册短密码（7位）-> 400 密码策略提示
$st = 0; $bd = $null
TryPost "$base/api/auth/register" @{username = "pwd_short_$stamp"; password = "Pass123"} ([ref]$st) ([ref]$bd)
Check "注册短密码 -> 400 密码提示" ($st -eq 400 -and $bd.error -like "*密码*") "status=$st error=$($bd.error)"
Start-Sleep -Seconds 21

# 4. 注册超长密码（33位）-> 400
$st = 0; $bd = $null
TryPost "$base/api/auth/register" @{username = "pwd_long_$stamp"; password = "P" * 33} ([ref]$st) ([ref]$bd)
Check "注册超长密码 -> 400" ($st -eq 400) "status=$st error=$($bd.error)"
Start-Sleep -Seconds 21

# 5. 重复注册 A -> 400 用户名已存在
$st = 0; $bd = $null
TryPost "$base/api/auth/register" @{username = $ua; password = $PWD_OK} ([ref]$st) ([ref]$bd)
Check "重复注册 -> 400 用户名已存在" ($st -eq 400 -and $bd.error -eq "用户名已存在") "status=$st error=$($bd.error)"
Start-Sleep -Seconds 21

# 6. 并发注册同名账号：一个 200 一个 400，不允许 500
$cuser = "race_$stamp"
$scriptBlock = { param($b, $u, $p, $xf)
    try {
        Invoke-RestMethod -Uri "$b/api/auth/register" -Method Post -ContentType "application/json" -Headers @{ "X-Forwarded-For" = $xf } -Body (@{username = $u; password = $p} | ConvertTo-Json) | Out-Null
        return "200"
    } catch { return "$($_.Exception.Response.StatusCode.value__)" }
}
$job1 = Start-Job -ArgumentList $base, $cuser, $PWD_OK, "203.0.113.101" -ScriptBlock $scriptBlock
$job2 = Start-Job -ArgumentList $base, $cuser, $PWD_OK, "203.0.113.102" -ScriptBlock $scriptBlock
Wait-Job $job1, $job2 -Timeout 60 | Out-Null
$codes = @((Receive-Job $job1), (Receive-Job $job2) | ForEach-Object { [int]$_ })
Remove-Job $job1, $job2
Check "并发注册同名 -> 200+400 无500" (($codes -contains 200) -and ($codes -contains 400) -and -not ($codes -contains 500)) "codes=$($codes -join ',')"
Start-Sleep -Seconds 21

# 7. 注册合法账号 B（登录测试用）
$st = 0; $bd = $null
TryPost "$base/api/auth/register" @{username = $ub; password = $PWD_OK} ([ref]$st) ([ref]$bd)
Check "注册合法账号B -> 200" ($st -eq 200) "status=$st"

# ---- 登录类 ----

# 8. 正确密码登录 -> 200 token
$st = 0; $bd = $null
TryPost "$base/api/auth/login" @{username = $ua; password = $PWD_OK} ([ref]$st) ([ref]$bd)
Check "正确密码登录 -> 200 token" ($st -eq 200 -and $bd.token) "status=$st"

# 9. 错误密码 -> 401 统一提示（不区分）
$st = 0; $bd = $null
TryPost "$base/api/auth/login" @{username = $ua; password = "WrongPass1"} ([ref]$st) ([ref]$bd)
Check "错误密码 -> 401 用户名或密码错误" ($st -eq 401 -and $bd.error -eq "用户名或密码错误") "status=$st error=$($bd.error)"

# 10. 不存在用户 -> 401 同样提示（防枚举）
$st = 0; $bd = $null
TryPost "$base/api/auth/login" @{username = "no_such_user_$stamp"; password = $PWD_OK} ([ref]$st) ([ref]$bd)
Check "不存在用户 -> 401 同提示(防枚举)" ($st -eq 401 -and $bd.error -eq "用户名或密码错误") "status=$st error=$($bd.error)"

# 11. 空用户名 -> 400 请输入用户名
$st = 0; $bd = $null
TryPost "$base/api/auth/login" @{username = ""; password = $PWD_OK} ([ref]$st) ([ref]$bd)
Check "空用户名 -> 400 提示" ($st -eq 400 -and $bd.error -eq "请输入用户名") "status=$st error=$($bd.error)"

# 12. 空密码 -> 400 请输入密码
$st = 0; $bd = $null
TryPost "$base/api/auth/login" @{username = $ua; password = ""} ([ref]$st) ([ref]$bd)
Check "空密码 -> 400 提示" ($st -eq 400 -and $bd.error -eq "请输入密码") "status=$st error=$($bd.error)"

# ---- 锁定测试（放最后：5 次失败后锁 15 分钟，专用账号） ----

# 13. 连续 5 次错误密码，第 6 次 -> 429 账号已锁定（专用假 IP，避开与登录用例共享的 IP 限流窗口）
$lockHeaders = @{ "X-Forwarded-For" = "203.0.113.200" }
for ($i = 1; $i -le 5; $i++) {
    $st = 0; $bd = $null
    TryPost "$base/api/auth/login" @{username = $ud; password = "WrongPass1"} ([ref]$st) ([ref]$bd) $lockHeaders | Out-Null
}
$st = 0; $bd = $null
TryPost "$base/api/auth/login" @{username = $ud; password = "WrongPass1"} ([ref]$st) ([ref]$bd) $lockHeaders
Check "连续5次失败后 -> 429 账号已锁定" ($st -eq 429 -and $bd.error -like "*锁定*") "status=$st error=$($bd.error)"

Write-Host ""
if ($script:fail -eq 0) { Write-Host "===== 认证验收全部通过 =====" } else { Write-Host "===== 有 $($script:fail) 项失败 ====="; exit 1 }
